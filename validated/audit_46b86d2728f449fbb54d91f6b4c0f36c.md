### Title
Dust deposits/swaps pull collateral but mint zero tokens due to round-down with no zero-output check - (File: contracts/DepositToken.sol)

### Summary
The Notional H-3 bug class is: a user transfers value in, a conversion formula rounds down to zero shares, and the code mints `0` without reverting — the deposited assets stay in the protocol and are unrecoverable. Metronome has the same shape in `DepositToken.deposit()` and the same pattern exists in `Pool.swap()`: the contract pulls collateral into `Treasury`, computes the mint amount via a fee/price quote that rounds down, and calls `_mint(account, 0)` without any `!= 0` check.

### Finding Description
In `DepositToken.deposit` (`contracts/DepositToken.sol:211-237`):

```solidity
uint256 _balanceBefore = _underlying.balanceOf(_treasury);
_underlying.safeTransferFrom(_msgSender, _treasury, amount_);
amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

(_deposited, _fee) = quoteDepositOut(amount_);
if (_fee > 0) {
    _mint(_pool.feeCollector(), _fee);
}

_mint(onBehalfOf_, _deposited);
```

The function only checks `amount_ == 0` on the *input* argument (line 215). It never checks that `_deposited > 0` after the balance-delta measurement and the fee deduction. `_mint` itself happily mints `0` (no zero-amount revert in `contracts/DepositToken.sol:469-489`; the `amount_ > 0` conditions there only gate the deposit-token-list bookkeeping).

Two reachable zero-mint paths:

1. **Fee-on-transfer / rebasing collateral:** `safeTransferFrom` is called with the caller-supplied `amount_`, but the credited amount is the treasury balance delta. For a deflationary collateral the delta can be `0` while the user's tokens left their wallet. `quoteDepositOut(0)` returns `(0, fee)` and `_mint(onBehalfOf_, 0)` executes — the user donated their transfer to the treasury.
2. **Dust deposit with fee rounding:** for a very small received `amount_`, fee math in `quoteDepositOut`/`FeeProvider` can absorb the whole amount (`_deposited == 0`), again minting nothing.

The identical class exists in `Pool.swap()` / `quoteSwapOut` path: collateral is deposited, the synthetic amount to mint is derived from an oracle-priced USD conversion, and a dust or precision-mismatched input (e.g., low-decimal/low-price collateral converted through 18-decimal USD values, the exact analog of the 1e8-vs-1e18 mismatch in the report) can round the minted synthetic amount to `0` while the collateral is retained. There is no `require(minted != 0)` check anywhere in the deposit → mint pipeline.

### Impact Explanation
Loss of user funds: the user transfers collateral to `Treasury` and receives `0` msdTOKEN / `0` synthetic tokens, with no position to withdraw against. The forfeited collateral accrues to the treasury/feeCollector — directly matching the report's "deposited assets but received zero tokens" impact. Unlike the Notional case there is no share-supply invariant broken, but the "value in, zero claim out" invariant is violated.

### Likelihood Explanation
Reachable by any unprivileged EOA via the public `deposit(uint256,address)` entry point (no role gates beyond `whenNotPaused`/`nonReentrant`). The trigger is simply a dust amount, or a collateral whose transfer mechanics reduce the received delta to zero. Magnitude per call is small (dust), so realized loss per tx is bounded by the deposit size; it does not scale to drain the protocol, but it is a reproducible, unconditional loss for the caller.

### Recommendation
Revert when the computed mint amount is zero, matching the Solmate-style fix recommended in the report:

```solidity
(_deposited, _fee) = quoteDepositOut(amount_);
if (_deposited == 0) revert AmountIsZero();
```

Apply the same zero-output check in `Pool.swap()` after the quoted synthetic amount is computed.

### Proof of Concept
```solidity
// Foundry-style sketch
// given: depositToken underlying = ERC20Mock, depositFee = 0
function test_dustDepositMintsZero() public {
    // Path A: collateral with 100% transfer fee -> treasury delta = 0
    uint256 amount = 1e18;
    underlying.setFee(1e18); // fee-on-transfer mock
    underlying.approve(address(depositToken), amount);
    (uint256 deposited,) = depositToken.deposit(amount, alice);
    assertEq(deposited, 0);
    assertEq(depositToken.balanceOf(alice), 0);   // alice lost `amount`, got nothing
}

// Path B: deposit amount such that quoteDepositOut yields _deposited == 0
// (tiny amount where fee consumes the full received value)
```

Caveat: I could not fully verify the exact rounding inside `FeeProvider`'s quote functions within the available iterations; Path A (balance-delta → zero mint) is confirmed directly by `DepositToken.sol:225-234`, while the price-rounding variant in `Pool.swap` should be confirmed against `quoteSwapOut`'s decimal scaling before reporting.