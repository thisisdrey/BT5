### Title
`Pool.liquidate` provides no slippage protection on the collateral seized for a fixed synth burn, so a liquidator can receive less collateral than quoted - (contracts/Pool.sol)

### Summary
Metronome's `liquidate` has the same missing-slippage-check bug class as the referenced finding. The liquidator commits a fixed `amountToRepay_` of synthetic token to burn, but the amount of collateral they receive is priced entirely at execution time by `masterOracle().quote` inside `quoteLiquidateOut`. There is no `minCollateralOut` / `maxRepay` parameter to let the liquidator bound the exchange rate, so any unfavorable oracle price movement between transaction construction and execution directly reduces the collateral received for the same burned synth.

### Finding Description
`liquidate` burns `amountToRepay_` of `syntheticToken_` from the caller and seizes collateral computed via the live oracle quote (`quoteLiquidateOut` → `masterOracle().quote(synthetic, underlying, amountToRepay_)`, then adds `liquidatorIncentive` and `protocolFee`). Because the function takes no bound parameter, the effective liquidation price is whatever the oracle returns in the execution block — identical to the Tangent `minUSGOut` gap where `minUSGOut` is only enforced on the zap path.

```solidity
(_totalSeized, _toLiquidator, _fee) = quoteLiquidateOut(syntheticToken_, amountToRepay_, depositToken_);
...
syntheticToken_.burn(_msgSender, amountToRepay_);
depositToken_.seize(account_, _msgSender, _toLiquidator);
```

- `Pool.liquidate`: contracts/Pool.sol:537-596 — no slippage parameter.
- `quoteLiquidateOut`: contracts/Pool.sol:451-472 — oracle-priced seize amount.
- `IPool.liquidate` signature: contracts/interfaces/IPool.sol:63-68 — confirms no bound input.

The same exposure exists for a sandwich scenario: if the oracle path for a collateral (e.g., vault-share-priced deposits routed through `MasterOracle`/`VesperGateway` share prices, or any pool-backed quote) can be nudged within a block, an unprivileged attacker can front-run a pending `liquidate` call, depress the synth/collateral exchange rate so `_toLiquidator` shrinks, let the liquidation execute at the bad rate, and back-run the price — the burn is fixed, the seized amount is not. Even without active manipulation, normal oracle update latency between quote and execution yields the same unbounded loss.

### Impact Explanation
Liquidators burning their own synth can receive materially less deposit-token collateral than expected — direct loss of user (liquidator) funds on a public entry point. Incentive margin (~`liquidatorIncentive`) is thin, so small price deviations turn a profitable liquidation into a loss.

### Likelihood Explanation
Every `liquidate` call is exposed; no modifier (`whenNotShutdown`, `nonReentrant`, existence checks) constrains the executed price. Exploitation requires oracle price movement (natural latency or same-tx manipulation where the feed path permits) plus a pending liquidation tx — moderate likelihood, bounded loss per event.

### Recommendation
Add a `minCollateralOut_` (or `maxRepayInUsd_`) parameter to `liquidate` and revert if `_toLiquidator`/the effective rate is below the caller's bound, e.g. `if (_toLiquidator < minCollateralOut_) revert SlippageExceeded();`.

### Proof of Concept
Foundry fork sketch:

```solidity
// setup: alice position underwater; liquidator holds msEth
uint256 amountToRepay = pool.quoteLiquidateMax(msEth, alice, msdMET);
(, uint256 expectedToLiquidator,) = pool.quoteLiquidateOut(msEth, amountToRepay, msdMET);

// attacker (or oracle update) moves synthetic->collateral quote against liquidator
// e.g. push collateral underlying price up / synth price down on the oracle feed path

vm.prank(liquidator);
(, uint256 toLiquidator,) = pool.liquidate(msEth, alice, amountToRepay, msdMET);

// liquidator burned the same `amountToRepay` but received less collateral
assertLt(toLiquidator, expectedToLiquidator); // no minCollateralOut check existed to revert
```

A fork test only needs to (1) snapshot `quoteLiquidateOut` at time T, (2) shift the master-oracle feed price for the collateral/synth pair, (3) execute `liquidate` with the same `amountToRepay_`, showing the call succeeds at the worse rate instead of reverting. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** contracts/Pool.sol (L451-472)
```text
    function quoteLiquidateOut(
        ISyntheticToken syntheticToken_,
        uint256 amountToRepay_,
        IDepositToken depositToken_
    ) public view override returns (uint256 _totalToSeize, uint256 _toLiquidator, uint256 _fee) {
        _toLiquidator = masterOracle().quote(
            address(syntheticToken_),
            address(depositToken_.underlying()),
            amountToRepay_
        );

        (uint128 _liquidatorIncentive, uint128 _protocolFee) = feeProvider.liquidationFees();

        if (_protocolFee > 0) {
            _fee = _toLiquidator.wadMul(_protocolFee);
        }
        if (_liquidatorIncentive > 0) {
            _toLiquidator += _toLiquidator.wadMul(_liquidatorIncentive);
        }

        _totalToSeize = _fee + _toLiquidator;
    }
```

**File:** contracts/Pool.sol (L537-596)
```text
    function liquidate(
        ISyntheticToken syntheticToken_,
        address account_,
        uint256 amountToRepay_,
        IDepositToken depositToken_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfSyntheticTokenExists(syntheticToken_)
        onlyIfDepositTokenExists(depositToken_)
        returns (uint256 _totalSeized, uint256 _toLiquidator, uint256 _fee)
    {
        address _msgSender = _msgSender();

        if (amountToRepay_ == 0) revert AmountIsZero();
        if (_msgSender == account_) revert CanNotLiquidateOwnPosition();

        IDebtToken _debtToken = debtTokenOf[syntheticToken_];
        _debtToken.accrueInterest();

        (bool _isHealthy, , , , ) = debtPositionOf(account_);

        if (_isHealthy) {
            revert PositionIsHealthy();
        }

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
    }
```

**File:** contracts/interfaces/IPool.sol (L63-68)
```text
    function liquidate(
        ISyntheticToken syntheticToken_,
        address account_,
        uint256 amountToRepay_,
        IDepositToken depositToken_
    ) external returns (uint256 _totalSeized, uint256 _toLiquidator, uint256 _fee);
```
