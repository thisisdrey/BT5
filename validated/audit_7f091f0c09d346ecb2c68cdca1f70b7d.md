### Title
Dust deposits/transfers fill a victim's per-account token list to `MAX_TOKENS_PER_USER`, DoS-ing their deposits, mints, and top-ups (File: contracts/Pool.sol)

### Summary
Analogous to the advisory's "crafted input causes a denial of service" class: an unprivileged attacker can force entries into a victim's `depositTokensOfAccount`/`debtTokensOfAccount` list without consent, causing every subsequent position-changing call for that account to revert with `UserReachedMaxTokens` once the combined count reaches 30.

### Finding Description
`Pool.addToDepositTokensOfAccount` is gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (30) [1](#0-0) . The victim has no way to opt out of entries being added on their behalf:

1. `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint msdTOKEN to an arbitrary `onBehalfOf_` [2](#0-1) . `_mint` then calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's prior balance was zero and `amount_ > 0` [3](#0-2) .
2. `DepositToken.transfer`/`transferFrom`/`seize` reach `_transfer`, which performs the same unsolicited `addToDepositTokensOfAccount(recipient_)` for first-time recipients [4](#0-3) .

An attacker therefore deposits 1 wei (or transfers dust) of every registered `DepositToken` to a target victim. Once the victim's combined deposit + debt token count hits 30, the following all revert for the victim:
- Any new `deposit()` into a collateral they don't already hold (`_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`).
- Any new `DebtToken` issue/mint (`issue`/`mint` → `addToDebtTokensOfAccount`, same modifier) — the victim cannot open a new borrow or swap into a new synthetic, since `Pool.swap` also mints.

Critically, the victim cannot remove the dust entries themselves: `removeFromDepositTokensOfAccount` is only callable by the `DepositToken` contract when the balance hits zero, so the victim must spend gas to transfer/withdraw every dust token one by one [5](#0-4) . The attacker can front-run the victim's cleanup transactions and re-fill freed slots in the same or next block.

### Impact Explanation
Temporary denial of service / induced loss of funds. The strongest impact path: a victim whose position is drifting toward liquidation attempts to deposit additional collateral to restore health. The attacker fills the victim's token list (dust deposits via `onBehalfOf_` are enough if the victim already holds several tokens) and front-runs the deposit tx — the rescue deposit reverts with `UserReachedMaxTokens`, and the victim is liquidated in the same block window, losing collateral to the liquidator/seize path in `Pool.liquidate`/`DepositToken.seize`. Absent liquidation, the impact is temporary freezing of protocol functionality for that account (no new deposits, borrows, or swaps) until the victim manually empties each dust balance — a griefing the attacker can repeat at dust cost.

### Likelihood Explanation
- Attacker is unprivileged: only needs to call public `DepositToken.deposit(amount_, victim)` or `transfer(victim, dust)` for each registered deposit token. No governance, keeper, or oracle involvement.
- Reachability depends on the pool having enough registered `DepositToken`s such that `victim_existing_entries + n_depositTokens ≥ 30`. With fewer registered tokens the attack only works against victims already near the cap (e.g., users of many collateral types + many synthetic debts).
- Cost is negligible (dust amounts); cleanup cost and repetition advantage favor the attacker.
- Not stopped by `whenNotPaused`, `nonReentrant`, SynthContext sender checks, or supply caps — all checks pass since these are legitimate dust transfers.

### Recommendation
- Make the cap count only "material" balances (e.g., require a minimum amount in the underlying token before `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` is invoked), or
- Require recipient consent: skip list insertion for `onBehalfOf_ != _msgSender()` mints and inbound `_transfer` below a dust threshold, or
- Allow the account itself to prune its own list (a `Pool.removeMyDepositToken(token)` escape hatch callable by the account, not only by the token contract).

### Proof of Concept
Foundry/Hardhat fork sketch:

```solidity
// Setup: pool with N registered DepositTokens dToken[0..N-1], victim holds
// some deposit+debt tokens such that N + existing >= 30.
// (test/Pool.test.ts L1386-1416 demonstrates the revert boundary.)

function test_maxTokensGriefing() public {
    address victim = makeAddr("victim");
    // victim already has a position (debt + a few collaterals)
    uint256 existing = pool.debtTokensOfAccount(victim).length
                     + pool.depositTokensOfAccount(victim).length;

    // Attacker dust-deposits every DepositToken to victim
    for (uint256 i; i < depositTokens.length && existing + i < 30; ++i) {
        underlying[i].approve(address(depositTokens[i]), 1);
        depositTokens[i].deposit(1, victim); // onBehalfOf_ = victim
    }

    // Victim's deposit into a collateral they don't yet hold now reverts
    vm.prank(victim);
    underlyingNew.approve(address(dTokenNew), 1e18);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    dTokenNew.deposit(1e18, victim);

    // Same for issuing a new synthetic debt
    vm.prank(victim);
    vm.expectRevert(Pool.UserReachedMaxTokens.selector);
    debtTokenNew.issue(1e18, victim);

    // Victim cannot call removeFromDepositTokensOfAccount themselves;
    // must transfer each dust token out to free slots.
    vm.prank(victim);
    vm.expectRevert(Pool.SenderIsNotDepositToken.selector);
    pool.removeFromDepositTokensOfAccount(victim);
}
```

The revert boundary and the "only the token contract may remove" invariant are already exercised by existing tests at `test/Pool.test.ts` L1386-1416 and L1424-1440.

### Citations

**File:** contracts/Pool.sol (L143-148)
```text
    modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
        if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
            revert UserReachedMaxTokens();
        }
        _;
    }
```

**File:** contracts/DepositToken.sol (L211-237)
```text
    function deposit(
        uint256 amount_,
        address onBehalfOf_
    ) external override whenNotPaused nonReentrant onlyIfDepositTokenExists returns (uint256 _deposited, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();
        if (onBehalfOf_ == address(0)) revert BeneficiaryIsNull();

        IPool _pool = pool;
        IERC20 _underlying = underlying;
        address _msgSender = _msgSender();
        address _treasury = address(_pool.treasury());

        if (_msgSender == _treasury) revert TreasuryCanNotDeposit();

        uint256 _balanceBefore = _underlying.balanceOf(_treasury);
        _underlying.safeTransferFrom(_msgSender, _treasury, amount_);
        amount_ = _underlying.balanceOf(_treasury) - _balanceBefore;

        (_deposited, _fee) = quoteDepositOut(amount_);
        if (_fee > 0) {
            _mint(_pool.feeCollector(), _fee);
        }

        _mint(onBehalfOf_, _deposited);

        emit CollateralDeposited(_msgSender, onBehalfOf_, amount_, _deposited, _fee);
    }
```

**File:** contracts/DepositToken.sol (L460-462)
```text
        if (_amount > 0 && _balanceAfter == 0) {
            pool.removeFromDepositTokensOfAccount(_account);
        }
```

**File:** contracts/DepositToken.sol (L486-488)
```text
        if (_balanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(account_);
        }
```

**File:** contracts/DepositToken.sol (L517-525)
```text
        // Add this token to the deposit tokens list if the recipient is receiving it for the 1st time
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }

        // Remove this token from the deposit tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf[sender_] == 0) {
            pool.removeFromDepositTokensOfAccount(sender_);
        }
```
