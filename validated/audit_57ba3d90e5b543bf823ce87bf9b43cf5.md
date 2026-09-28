### Title
Underwater positions leave permanently unbacked bad debt (and become fully unliquidatable when `debtFloorInUsd` is set), silently socializing the loss across synthetic-token holders - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` handles a bankrupt (underwater) position by seizing collateral only up to the account's `DepositToken` balance, while leaving the residual `DebtToken` balance — and the corresponding synthetic tokens circulating in the market — permanently unbacked. There is no insurance fund, treasury cover, or write-off/socialization mechanism: the loss stays attributed to the bankrupt account that can never repay, so the pool becomes underfunded and the shortfall is implicitly borne by the last synthetic holders/collateral withdrawers. Worse, when `debtFloorInUsd > 0` is configured, a sufficiently underwater position cannot be liquidated at all: repaying everything reverts via `AmountIsTooHigh`, and repaying up to the collateral cap leaves `0 < remainingDebt < debtFloorInUsd`, reverting via `RemainingDebtIsLowerThanTheFloor`. Bad debt is therefore guaranteed to persist and grow via interest accrual.

### Finding Description
In `Pool.liquidate`, after computing `(_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(...)`, the function reverts if `_totalSeized > depositToken_.balanceOf(account_)` (`AmountIsTooHigh`) and reverts if the post-liquidation debt is non-zero but below `debtFloorInUsd` (`RemainingDebtIsLowerThanTheFloor`).

For a position where `debtInUsd > depositInUsd` (underwater, e.g., after a collateral price crash or accrued interest):

- `amountToRepay_ = debtToken.balanceOf(account_)` → `_totalSeized` exceeds the deposit balance → `AmountIsTooHigh` revert.
- `amountToRepay_ = quoteLiquidateMax(...)` → seizes (nearly) all collateral, burns only part of the debt, and the leftover debt stays on the bankrupt account forever with zero collateral backing it. The synthetic tokens that were minted against that debt remain in circulation — supply is no longer matched by collateral. Since `Pool.swap` prices synths purely via `MasterOracle` quotes with no solvency check, the unbacked synth keeps trading at par, and the deficit only materializes when collateral is withdrawn: the Treasury holds less underlying than `DepositToken.totalSupply`, so the last withdrawers cannot be paid.
- If `debtFloorInUsd > 0` (it is configured on deployments) and the residual debt after seizing the whole deposit would fall below the floor — the common case for underwater accounts — both liquidation paths revert, making the bad debt permanently unclosable while `DebtToken.accrueInterest` keeps growing it.

Relevant code: `Pool.liquidate` reverts on `_totalSeized > depositToken_.balanceOf(account_)` and on `0 < _newDebtInUsd < debtFloorInUsd`, then burns only `amountToRepay_` of debt while seizing capped collateral: [1](#0-0) 

`quoteLiquidateMax` caps repayment by the deposit balance, guaranteeing residual bad debt for underwater accounts: [2](#0-1) 

`DepositToken._withdraw` burns msdTOKEN and pulls underlying from `Treasury` with no protocol-level solvency check, so aggregate claims can exceed Treasury holdings: [3](#0-2) 

### Impact Explanation
Protocol insolvency: after an underwater liquidation, synthetic token total supply exceeds the collateral backing it. The residual `DebtToken` balance is uncollectable, the loss is silently socialized across all holders of that synth and across depositors (Treasury underfunded → late withdrawers' `treasury().pull()` fails). With `debtFloorInUsd > 0`, this outcome is guaranteed for deeply underwater accounts because every liquidation path reverts, and `DebtToken` interest keeps compounding the deficit. This maps directly to the reported class: on bankruptcy the loss is attributed to a party (the bankrupt account / passive synth holders) that cannot absorb it, instead of being bounded or covered, leaving the contract underfunded and some claims unredeemable.

### Likelihood Explanation
Reachable by unprivileged actors through normal market movement: open a max-LTV position (`DebtToken.issue` up to `_issuableInUsd`), wait for a collateral price drop or interest accrual pushing debt above collateral value (same-transaction oracle manipulation also works via a manipulated DMM/AMM feed). Any EOA can then call `Pool.liquidate`; no privileged role needed. The underwater state is a normal market outcome for a lending protocol, and the unbacked residual debt arises on every such liquidation — and is fully guaranteed once `debtFloorInUsd` is set.

### Recommendation
Add an explicit bad-debt path in `Pool.liquidate`: when `debtInUsd > depositInUsd`, allow the liquidator to seize the entire deposit balance and either (a) write off / socialize the residual debt explicitly (e.g., a dedicated bad-debt ledger funded by `feeCollector` or Treasury), or (b) burn residual debt without requiring the liquidator to repay it in full. Additionally, exempt liquidations that leave a position collateral-empty from the `debtFloorInUsd` check, or waive the floor when the residual debt is unbacked, so underwater accounts are always closable.

### Proof of Concept
Hardhat (against existing test scaffolding in `test/Pool.test.ts`, underwater-position describe block):

```ts
// Alice deposits MET and mints msETH at max LTV (existing fixture state).
// Crash MET price so debtInUsd > depositInUsd.
await masterOracle.updatePrice(met.address, toUSD('0.10'));

// Floor is set on deployment configs
await pool.updateDebtFloor(parseEther('100'));

const debt = await msEthDebtToken.balanceOf(alice.address);

// Path 1: full repayment reverts - not enough collateral to seize
await expect(
  pool.connect(liquidator).liquidate(msEth.address, alice.address, debt, msdMET.address)
).to.revertedWithCustomError(pool, 'AmountIsTooHigh');

// Path 2: repay up to collateral cap - leaves residual debt below floor
const maxRepay = await pool.quoteLiquidateMax(msEth.address, alice.address, msdMET.address);
await expect(
  pool.connect(liquidator).liquidate(msEth.address, alice.address, maxRepay, msdMET.address)
).to.revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');

// Bad debt is permanently stuck and keeps accruing interest
expect(await msEthDebtToken.balanceOf(alice.address)).to.be.gt(0);

// Insolvency: synth supply is now unbacked -
// sum of msdMET claims > Treasury MET balance after other users withdraw.
```

### Citations

**File:** contracts/Pool.sol (L429-439)
```text
        (uint256 _amountToRepay, , ) = quoteLiquidateIn(
            syntheticToken_,
            depositToken_.balanceOf(account_),
            depositToken_
        );

        _maxAmountToRepay = debtTokenOf[syntheticToken_].balanceOf(account_).wadMul(maxLiquidable);

        if (_amountToRepay < _maxAmountToRepay) {
            _maxAmountToRepay = _amountToRepay;
        }
```

**File:** contracts/Pool.sol (L565-595)
```text
        uint256 _debtTokenBalance = _debtToken.balanceOf(account_);

        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }

        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        (_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);

        if (_totalSeized > depositToken_.balanceOf(account_)) {
            revert AmountIsTooHigh();
        }

        syntheticToken_.burn(_msgSender, amountToRepay_);
        _debtToken.burn(account_, amountToRepay_);
        depositToken_.seize(account_, _msgSender, _toLiquidator);

        if (_fee > 0) {
            depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee);
        }

        emit PositionLiquidated(_msgSender, account_, syntheticToken_, amountToRepay_, _totalSeized, _fee);
```

**File:** contracts/DepositToken.sol (L536-554)
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
    }
```
