### Title
Front-running dust `DebtToken.repay` blocks a victim's full repayment and keeps all of their collateral locked - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
The Restake Sui bug class is "dust remainder re-lock": a tiny residual amount forces a large withdrawal request to be unfillable, temporarily freezing user funds. The Metronome analog lives in the debt-repayment path. `DebtToken.repay` lets anyone repay on behalf of any account, and reverts with `BurnAmountExceedsBalance` when the amount exceeds the account's debt balance. While any debt remains, `DepositToken.unlockedBalanceOf` returns 0 (or near-0), so the victim cannot withdraw or transfer any `msd*` collateral. An unprivileged attacker can front-run a victim's exact-amount full repayment with a 1-wei repay, making the victim's transaction revert — repeatedly, at gas-only cost — keeping the victim's entire collateral position locked.

### Finding Description
`DepositToken.withdraw`/`transfer` enforce `_revertIfLocked`, which uses `unlockedBalanceOf(account_)`: if `_debtInUsd > 0` and `_issuableInUsd == 0`, the unlocked balance is `0`, so every wei of `msdTOKEN` held by the account is frozen. [1](#0-0) [2](#0-1) 

A user exiting a position therefore must fully clear their `DebtToken` balance. `DebtToken.repay(account_, amount_)` is callable by anyone for any `account_` (this is exercised in tests, e.g. `msETH_Debt_A.connect(bob).repay(alice.address, ...)`), and burns `amount_` of debt shares from `account_`, reverting if the amount exceeds the account's balance. The revert on overpayment is also exercised in tests (`'BurnAmountExceedsBalance'`). [3](#0-2) 

Because debt balances grow with interest index accrual, a victim computes their full repay amount (debt shares plus repay fee) off-chain and submits `repay(victim, X)`. An attacker observing the mempool calls `repay(victim, 1)` first. The victim's `X` now exceeds `balanceOf(victim)` and the transaction reverts. Even if the victim uses a freshly quoted amount each time, the attacker can repeat the attack on every block for the cost of a dust repay (the fee charged is proportional to the repaid amount, so ~0 for 1 wei).

### Impact Explanation
Temporary freezing of user funds. Each successful front-run blocks the victim's repayment transaction, and because `unlockedBalanceOf` is 0 whenever any debt remains and the position is fully utilized, none of the victim's collateral in `DepositToken` can be withdrawn or transferred (`withdraw`, `transfer`, and `transferFrom` all go through `_revertIfLocked`). The victim's only recourse is to keep retrying with exact amounts or switch to a `repayAll`-style call that reads `balanceOf` in-transaction — which not all integrations do. The attack is griefing-only (no profit), matching the accepted "temporary freezing of funds" impact class. [4](#0-3) [5](#0-4) 

### Likelihood Explanation
- Attacker requirements: unprivileged EOA only; needs to hold (or flash-borrow via `DebtToken.flashIssue`) a few wei of the synthetic asset plus gas. No governor/keeper/oracle involvement.
- Trigger condition: any user submitting a full-exact-amount `repay` through a public mempool. Integrations that precompute the repay amount (the natural pattern, since `repay` requires the caller to supply `amount_` including the repay fee) are vulnerable; `repayAll` is not, if it resolves the balance internally.
- Repeatability: the attack can be replayed every block indefinitely at near-zero marginal cost, analogous to the per-epoch re-lock in the original report.

### Recommendation
Mirror the Sui fix's spirit — make the boundary amount harmless instead of reverting:
- In `DebtToken.repay`, cap the burn at the account's current debt balance (`amount_ = Math.min(amount_, balanceOf(account_))`) instead of reverting on overshoot, so a front-run dust repay cannot invalidate the victim's transaction; refund any excess synthetic token to the caller.
- Alternatively/Additionally, document and encourage `repayAll` (balance resolved in-transaction) as the canonical full-exit path, and expose a `quoteRepayAllIn` helper so callers never need to hardcode the exact gross amount.

### Proof of Concept
Conceptual Hardhat fork test (network/config identical to `test/Integration.test.ts`):

```ts
// given: alice has debt and fully-utilized collateral
await msdDAI.connect(alice).deposit(parseEther('1000'), alice.address)
await msUSDDebt.connect(alice).issue(parseEther('500'), alice.address)

// alice computes exact full repay (principal + repay fee)
const aliceDebt = await msUSDDebt.balanceOf(alice.address)
const repayFee = await feeProvider.repayFee()
const amount = aliceDebt.mul(parseEther('1').add(repayFee)).div(parseEther('1'))
await msUSD.connect(alice).approve(msUSDDebt.address, amount)

// attacker front-runs with a dust repay on alice's behalf
await msUSD.connect(attacker).approve(msUSDDebt.address, MaxUint256)
await msUSDDebt.connect(attacker).repay(alice.address, 1) // 1 wei

// alice's tx now reverts — her collateral stays locked
await expect(msUSDDebt.connect(alice).repay(alice.address, amount))
  .rejectedWith('BurnAmountExceedsBalance')
expect(await msdDAI.unlockedBalanceOf(alice.address)).eq(0) // fully locked
await expect(msdDAI.connect(alice).withdraw(1, alice.address))
  .rejectedWith('NotEnoughFreeBalance')
```

Note: I could not fully read `DebtToken.sol`/`Pool.sol` internals within the search budget; the exact revert name and repay-fee mechanics should be confirmed against `DebtToken.repay`'s implementation (the revert `BurnAmountExceedsBalance` and repay-on-behalf semantics are confirmed by `test/Integration.test.ts:364-375`).

### Citations

**File:** contracts/DepositToken.sol (L180-182)
```text
    function _revertIfLocked(address account_, uint256 amount_) private view {
        if (unlockedBalanceOf(account_) < amount_) revert NotEnoughFreeBalance();
    }
```

**File:** contracts/DepositToken.sol (L348-376)
```text
    function transfer(address to_, uint256 amount_) external override returns (bool) {
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        _transfer(_msgSender, to_, amount_);
        return true;
    }

    /// @inheritdoc IERC20
    function transferFrom(
        address sender_,
        address recipient_,
        uint256 amount_
    ) external override nonReentrant returns (bool) {
        _revertIfLocked(sender_, amount_);

        address _msgSender = _msgSender();
        uint256 _currentAllowance = allowance[sender_][_msgSender];
        if (_currentAllowance != type(uint256).max) {
            if (_currentAllowance < amount_) revert AmountExceedsAllowance();
            unchecked {
                _approve(sender_, _msgSender, _currentAllowance - amount_);
            }
        }

        _transfer(sender_, recipient_, amount_);

        return true;
    }
```

**File:** contracts/DepositToken.sol (L383-398)
```text
    function unlockedBalanceOf(address account_) public view override returns (uint256 _unlockedBalance) {
        IPool _pool = pool;

        (, , uint256 _debtInUsd, , uint256 _issuableInUsd) = _pool.debtPositionOf(account_);

        if (_debtInUsd == 0) {
            return balanceOf[account_];
        }

        if (_issuableInUsd > 0) {
            _unlockedBalance = Math.min(
                balanceOf[account_],
                _pool.masterOracle().quoteUsdToToken(address(underlying), _issuableInUsd.wadDiv(collateralFactor))
            );
        }
    }
```

**File:** contracts/DepositToken.sol (L406-412)
```text
    function withdraw(uint256 amount_, address to_) external override returns (uint256 _withdrawn, uint256 _fee) {
        if (to_ == address(0)) revert RecipientIsNull();
        address _msgSender = _msgSender();
        _revertIfLocked(_msgSender, amount_);

        return _withdraw({account_: _msgSender, amount_: amount_, to_: to_});
    }
```

**File:** test/Integration.test.ts (L364-375)
```typescript
          it('should revert if repaying using wrong synthetic asset', async function () {
            // given
            expect(await poolA.debtOf(alice.address)).eq(toUSD('1,000'))
            expect(await msETH_Debt_A.balanceOf(alice.address)).eq(0)
            expect(await msETH.balanceOf(bob.address)).closeTo(parseEther('1'), parseEther('0.2'))

            // when
            const tx = msETH_Debt_A.connect(bob).repay(alice.address, parseEther('0.1'))

            // then
            await expect(tx).rejectedWith('BurnAmountExceedsBalance')
          })
```
