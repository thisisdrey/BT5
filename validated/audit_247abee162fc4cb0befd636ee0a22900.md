### Title
Debt positions with size in (debtFloorInUsd, debtFloorInUsd / maxLiquidable] can never be liquidated — permanent bad-debt DoS - (File: contracts/Pool.sol)

### Summary
Analogous to CVE-2020-6098 (a specially crafted request triggering denial of service), an unprivileged attacker can craft a debt position of a specific size that makes `Pool.liquidate` always revert, permanently preventing liquidation of that position and guaranteeing bad debt for the protocol once the position goes underwater.

### Finding Description
`Pool.liquidate` enforces two constraints on `amountToRepay_` simultaneously:

1. The liquidation cap: `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts (`maxLiquidable` defaults to `0.5e18` = 50%). [1](#0-0) 
2. The debt floor: after repayment, the remaining debt must be either `0` or `>= debtFloorInUsd`, otherwise it reverts with `RemainingDebtIsLowerThanTheFloor`. [2](#0-1) 

A full repayment (`amountToRepay_ == balance`) leaves `0` remaining and satisfies the floor check, but violates `maxLiquidable` whenever `maxLiquidable < 1e18`. A partial repayment of at most `maxLiquidable` fraction leaves a remainder that is `< debtFloorInUsd` whenever the total debt is below `debtFloorInUsd / maxLiquidable`. Therefore, when `debtFloorInUsd > 0` and `maxLiquidable < 1e18`, any debt token balance with USD value in the range `(debtFloorInUsd, debtFloorInUsd / maxLiquidable]` is permanently unliquidatable — every possible `amountToRepay_` reverts.

An attacker reaches this state with purely public entry points: deposit collateral via `DepositToken.deposit`, then call `DebtToken.issue` with `amount_` chosen so the resulting debt USD value is just above `debtFloorInUsd` (the `issue` path itself enforces the floor at mint time in `DebtToken._mint` [3](#0-2) , so the attacker lands just above it). The attacker withdraws all unlocked collateral, and since `DebtToken` accrues interest automatically (`_calculateInterestAccrual` [4](#0-3) ), the position deterministically drifts underwater over time with no further attacker action.

### Impact Explanation
Once the position is unhealthy, no liquidator can ever repay it: partial repayments revert via `RemainingDebtIsLowerThanTheFloor` and full repayment reverts via `AmountGreaterThanMaxLiquidable`. A third party could call `repay`/`repayAll` on the account [5](#0-4) , but that burns the payer's own synthetic tokens without seizing any collateral, so there is no rational incentive to do so. The result is guaranteed, permanent bad debt: the debt remains backed by insufficient collateral, breaking protocol solvency and the "every unhealthy position is liquidatable" invariant. Because the position size is capped only by the floor window, the attacker can repeat this across many accounts/synthetics to accumulate material bad debt.

### Likelihood Explanation
High whenever `debtFloorInUsd > 0` is configured (its documented purpose is to keep liquidations profitable, so a nonzero value is expected) and `maxLiquidable < 1e18` (the default is 50%, and `updateMaxLiquidable` enforces an upper bound via `MaxLiquidableTooHigh`). The attack requires only an EOA, ordinary collateral, and patience for interest accrual or a collateral price decline — no privileged role, oracle manipulation, or front-running is needed.

### Recommendation
Resolve the constraint conflict in `liquidate`, e.g.:
- Allow full repayment to bypass `maxLiquidable` when the entire remaining debt would be cleared (`amountToRepay_ == _debtTokenBalance`), or
- Permit a partial repayment that leaves a remainder below the floor to automatically extend to full repayment / waive the floor check when `balance * maxLiquidable < debtFloorInUsd`, or
- Enforce a minimum debt strictly above `debtFloorInUsd / maxLiquidable` at issuance time in `DebtToken._mint` so the dead zone is unreachable.

### Proof of Concept
Hardhat-style reproduction (mirroring the existing `test/Pool.test.ts` liquidation setup):

```ts
it('debt in (floor, floor/maxLiquidable] can never be liquidated', async function () {
  // debtFloor = $3,000, maxLiquidable = 50% => dead zone is ($3,000, $6,000]
  await pool.updateDebtFloor(parseEther('3000'))

  // alice (attacker) deposits MET collateral and issues debt worth ~$4,000 (inside dead zone)
  await met.mint(alice.address, collateralAmount)
  await met.connect(alice).approve(msdMET.address, ethers.constants.MaxUint256)
  await msdMET.connect(alice).deposit(collateralAmount, alice.address)
  const debtAmount = parseEther('1') // 1 msETH @ $4,000
  await msEthDebtToken.connect(alice).issue(debtAmount, alice.address)

  // make position unhealthy (interest accrual or price drop)
  await masterOracle.updatePrice(met.address, toUSD('0.50'))
  const { _isHealthy } = await pool.debtPositionOf(alice.address)
  expect(_isHealthy).false

  const balance = await msEthDebtToken.balanceOf(alice.address)

  // partial repay (<= 50%) leaves < floor -> reverts
  await expect(
    pool.connect(liquidator).liquidate(msEth.address, alice.address, balance.div(2), msdMET.address)
  ).revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor')

  // full repay clears floor but exceeds maxLiquidable -> reverts
  await expect(
    pool.connect(liquidator).liquidate(msEth.address, alice.address, balance, msdMET.address)
  ).revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable')

  // every intermediate amount reverts too: position is permanently unliquidatable
})
```

### Citations

**File:** contracts/Pool.sol (L567-569)
```text
        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }
```

**File:** contracts/Pool.sol (L571-579)
```text
        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }
```

**File:** contracts/DebtToken.sol (L466-495)
```text
    function repayAll(
        address onBehalfOf_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists
        returns (uint256 _repaid, uint256 _fee)
    {
        accrueInterest();

        _repaid = balanceOf(onBehalfOf_);
        if (_repaid == 0) revert AmountIsZero();

        address _msgSender = _msgSender();
        ISyntheticToken _syntheticToken = syntheticToken;

        uint256 _amount;
        (_amount, _fee) = quoteRepayIn(_repaid);

        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, pool.feeCollector(), _fee);
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);

        emit DebtRepaid(_msgSender, onBehalfOf_, _amount, _repaid, _fee);
    }
```

**File:** contracts/DebtToken.sol (L551-565)
```text
    function _calculateInterestAccrual()
        private
        view
        returns (uint256 _interestAmountAccrued, uint256 _debtIndex, uint256 _lastTimestampAccrued)
    {
        _lastTimestampAccrued = lastTimestampAccrued;
        _debtIndex = debtIndex;

        if (block.timestamp > _lastTimestampAccrued) {
            uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
            if (_interestRateToAccrue > 0) {
                _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
                _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
            }
        }
```

**File:** contracts/DebtToken.sol (L583-588)
```text
        if (
            _debtFloorInUsd > 0 &&
            masterOracle_.quoteTokenToUsd(address(syntheticToken), _balanceBefore + amount_) < _debtFloorInUsd
        ) {
            revert DebtLowerThanTheFloor();
        }
```
