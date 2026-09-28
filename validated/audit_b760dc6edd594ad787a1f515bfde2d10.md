### Title
Residual bad debt left after liquidation keeps accruing interest, minting unbacked synthetic assets to `feeCollector` - (File: contracts/Pool.sol / contracts/DebtToken.sol)

### Summary
When `Pool.liquidate()` can seize at most the account's remaining collateral, any debt that exceeds the collateral's value ("bad debt") stays recorded on the account in `DebtToken.principalOf`. Because `DebtToken.accrueInterest()` grows `debtIndex` and `totalSupply_` proportionally to the *entire* outstanding supply — including unpayable bad debt — the protocol keeps minting interest-denominated synthetic tokens to `feeCollector` that are not backed by any collateral. This is the same bug class as the JOJO `handleDebt` report: bad debt is never written off and continues compounding.

### Finding Description
`Pool.liquidate()` only reverts if the collateral to seize exceeds the account's deposit balance (`_totalSeized > depositToken_.balanceOf(account_)` → `AmountIsTooHigh`) [1](#0-0) . A liquidator can therefore fully drain an account's collateral while residual debt remains on the account — there is no bad-debt write-off, insurance transfer, or socialization step anywhere in `Pool`, `DebtToken`, or `DepositToken`.

That residual debt continues to accrue interest: `balanceOf` computes `principal * debtIndex / debtIndexOf`, so the bad-debt account's balance keeps compounding forever [2](#0-1) . Worse, `accrueInterest` computes `_interestAmountAccrued = interestRatePerSecond * totalSupply_` where `totalSupply_` includes the bad debt, and then calls `syntheticToken.mint(pool.feeCollector(), _interestAmountAccrued + pendingInterestFee)` [3](#0-2) . The interest attributable to unpayable debt is minted as real, transferable synthetic tokens to `feeCollector`.

### Impact Explanation
Protocol insolvency / unbacked issuance. Synthetic assets minted to `feeCollector` as "interest" on bad debt have no collateral or repayment source behind them — the bad-debt account has zero deposits and will never repay. As volatile markets produce more underwater accounts, the share of `totalSupply_` that is bad debt grows, and `accrueInterest` compounds it: every accrual mints unbacked msAssets to the fee collector, directly diluting the collateral backing of all circulating synthetic tokens. This breaks the ≥1:1 collateralization invariant and can drive a depeg. (Note the `try/catch` only defers the mint into `pendingInterestFee` if minting fails; it does not stop the accrual.)

### Likelihood Explanation
- Bad debt arises from ordinary market moves: once an account's collateral value < debt, liquidators seize all remaining collateral and leave residual debt. No privileged action is needed — any EOA can call `liquidate` on an underwater account.
- `maxLiquidable` only caps the per-call repay fraction (`amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts) [4](#0-3) ; repeated calls still drain collateral to zero while debt remains.
- A nonzero `interestRate` is the normal deployed configuration, so every bad-debt account deterministically inflates unbacked supply on each `accrueInterest` (called by `issue`, `repay`, `liquidate`, or anyone, since `accrueInterest` is `public`).

### Recommendation
Add a bad-debt resolution path: when an account's collateral is fully seized and residual debt remains, write off (burn) the residual debt without burning synthetic supply, or cover it from a stability reserve / `pendingInterestFee` funded by past interest revenue — e.g., track a `badDebt` accumulator excluded from `totalSupply_` in `_calculateInterestAccrual` so unpayable debt stops compounding.

### Proof of Concept
Hardhat/foundry fork scenario (metronome-synth test harness):

```ts
// 1. alice deposits 400 USDC into msdUSDC, issues max msUSD
await msdUSDC.deposit(parseUnits('400', 6), alice.address);
const {_issuableInUsd} = await pool.debtPositionOf(alice.address);
await msUSDDebt.issue(_issuableInUsd, alice.address);

// 2. Collateral price crashes (fork: move oracle/USDC price or set interest 50% to push underwater)
await msUSDDebt.connect(governor).updateInterestRate(parseEther('0.5'));
await time.increase(time.duration.days(30));

// 3. bob liquidates repeatedly; seize is capped by alice's depositToken balance,
//    so all collateral is drained while residual debt remains
await pool.connect(bob).liquidate(msUSD.address, alice.address, repayAmt, msdUSDC.address);
expect(await msdUSDC.balanceOf(alice.address)).eq(0);
const residualDebt = await msUSDDebt.balanceOf(alice.address);
expect(residualDebt).gt(0); // bad debt, zero collateral

// 4. Bad debt keeps compounding and inflates unbacked supply
await time.increase(time.duration.years(1));
const supplyBefore = await msUSD.totalSupply();
const feeBefore = await msUSD.balanceOf(feeCollector.address);
await msUSDDebt.accrueInterest();
// interest on totalSupply_ (incl. alice's unpayable debt) minted to feeCollector
expect(await msUSD.balanceOf(feeCollector.address)).gt(feeBefore);
expect(await msUSDDebt.balanceOf(alice.address)).gt(residualDebt); // still accruing
```

The invariant break: `msUSD.totalSupply()` grows by interest on debt that can never be repaid, so circulating synthetic supply is no longer fully backed by collateral.

### Citations

**File:** contracts/Pool.sol (L567-569)
```text
        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }
```

**File:** contracts/Pool.sol (L581-585)
```text
        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }
```

**File:** contracts/DebtToken.sol (L156-178)
```text
    function accrueInterest() public override {
        (
            uint256 _interestAmountAccrued,
            uint256 _debtIndex,
            uint256 _lastTimestampAccrued
        ) = _calculateInterestAccrual();

        if (block.timestamp == _lastTimestampAccrued) {
            return;
        }

        lastTimestampAccrued = block.timestamp;

        if (_interestAmountAccrued > 0) {
            totalSupply_ += _interestAmountAccrued;
            debtIndex = _debtIndex;

            // Note: Address states where minting will fail (e.g. the token is inactive, it reached max supply, etc)
            try syntheticToken.mint(pool.feeCollector(), _interestAmountAccrued + pendingInterestFee) {
                pendingInterestFee = 0;
            } catch {
                pendingInterestFee += _interestAmountAccrued;
            }
```

**File:** contracts/DebtToken.sol (L196-205)
```text
    function balanceOf(address account_) public view override returns (uint256) {
        uint256 _principal = principalOf[account_];
        if (_principal == 0) {
            return 0;
        }

        (, uint256 _debtIndex, ) = _calculateInterestAccrual();

        // Note: The `debtIndex / debtIndexOf` gives the interest to apply to the principal amount
        return (_principal * _debtIndex) / debtIndexOf[account_];
```
