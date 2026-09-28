### Title
Withdrawal fees can be avoided through repeated small withdrawals - (File: contracts/DepositToken.sol)

### Summary
`DepositToken.withdraw` calculates the withdrawal fee using half-up `wadMul` and then subtracts it from the requested amount. When `amount * withdrawFee < 0.5e18`, the calculated fee rounds to zero. An unprivileged user can therefore split a withdrawal into sufficiently small chunks and withdraw the full balance without paying the configured withdrawal fee.

### Finding Description
`quoteWithdrawOut` calculates `_fee = amount_.wadMul(_withdrawFee)` and returns `amount_ - _fee`. [1](#0-0)  `wadMul` rounds half up, so a positive fee becomes zero whenever the product is less than `HALF_WAD`. [2](#0-1) 

The public `withdraw` path only checks that the requested amount is unlocked before calling `_withdraw`. [3](#0-2)  `_withdraw` transfers a fee only when the rounded fee is nonzero, burns `_withdrawn`, and sends that amount from Treasury. [4](#0-3) 

For example, with a `0.1%` withdraw fee (`1e15`), every withdrawal of `499` or fewer base units produces:

```solidity
(499 * 1e15 + 0.5e18) / 1e18 = 0
```

The user receives all `499` units and pays no fee. Repeating the operation scales the bypass to an arbitrary unlocked balance.

### Impact Explanation
The protocol’s configured withdrawal-fee invariant is broken: users can extract collateral without transferring the intended fee share to `feeCollector`. This causes direct loss of protocol fee revenue. The impact scales with the withdrawn amount and is limited mainly by transaction count, gas cost, token precision, and the configured fee.

### Likelihood Explanation
The attack requires no privileges, oracle manipulation, malicious contract dependency, or special account state beyond an unlocked deposit-token balance. It is practical where gas costs are low or where the fee-free amount is economically meaningful, particularly for low-decimal collateral. It does not work when `withdrawFee` is zero.

### Recommendation
Round the fee in the protocol’s favor, or equivalently calculate the user output directly with multiplication by the post-fee fraction:

```solidity
_amountToWithdraw = amount_.wadMul(1e18 - _withdrawFee);
_fee = amount_ - _amountToWithdraw;
```

For this formula, ordinary floor division is preferable to `wadMul`’s half-up rounding, because it makes the implied fee round up rather than allowing a positive proportional fee to become zero. Apply the same review to deposit, issue, repay, swap, and liquidation fee quote paths, which also use `wadMul`-based proportional fee calculations.

### Proof of Concept
A reproducible Foundry sequence is:

```solidity
// Assume:
// - depositToken.underlying() == collateral
// - feeProvider.withdrawFee() == 1e15 // 0.1%
// - attacker has no debt, so its full deposit-token balance is unlocked.

uint256 chunk = 499; // 499 * 1e15 < HALF_WAD
uint256 iterations = 1_000;

for (uint256 i; i < iterations; ++i) {
    (uint256 withdrawn, uint256 fee) =
        depositToken.withdraw(chunk, attacker);

    assertEq(withdrawn, chunk);
    assertEq(fee, 0);
}
```

Expected result:

- Total collateral received: `499_000` base units.
- Total deposit tokens burned: `499_000`.
- Total fee received by `feeCollector`: `0`.
- A single `499_000`-unit withdrawal would instead charge `499` base units at a `0.1%` fee.

### Citations

**File:** contracts/DepositToken.sol (L326-334)
```text
    function quoteWithdrawOut(uint256 amount_) public view override returns (uint256 _amountToWithdraw, uint256 _fee) {
        uint256 _withdrawFee = pool.feeProvider().withdrawFee();
        if (_withdrawFee == 0) {
            return (amount_, _fee);
        }

        _fee = amount_.wadMul(_withdrawFee);
        _amountToWithdraw = amount_ - _fee;
    }
```

**File:** contracts/DepositToken.sol (L406-411)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
```

**File:** contracts/DepositToken.sol (L536-553)
```text
    function _withdraw(
        address account_,
        uint256 amount_,
        address to_
    ) private whenNotShutdown nonReentrant onlyIfDepositTokenExists returns (uint256 _withdrawn, uint256 _fee) {
        if (amount_ == 0) revert AmountIsZero();

        IPool _pool = pool;

        (_withdrawn, _fee) = quoteWithdrawOut(amount_);
        if (_fee > 0) {
            _transfer(account_, _pool.feeCollector(), _fee);
        }

        _burn(account_, _withdrawn);
        _pool.treasury().pull(to_, _withdrawn);

        emit CollateralWithdrawn(account_, to_, amount_, _withdrawn, _fee);
```

**File:** contracts/lib/WadRayMath.sol (L25-31)
```text
    function wadMul(uint256 a, uint256 b) internal pure returns (uint256) {
        if (a == 0 || b == 0) {
            return 0;
        }

        return (a * b + HALF_WAD) / WAD;
    }
```
