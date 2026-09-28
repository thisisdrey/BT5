### Title
Oracle failure or zero collateral price freezes liquidations for otherwise liquidatable positions - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.liquidate` has no oracle-failure isolation. Before burning debt or seizing collateral, it queries the configured `IMasterOracle` for every debt token and collateral token in the account, then queries the oracle again to calculate the collateral seized. If any required quote reverts, returns an unusable zero price, or the oracle itself is unavailable, the liquidation transaction reverts and the position cannot be liquidated through the normal public path.

### Finding Description
`Pool.liquidate` first calls `debtPositionOf(account_)` to verify that the position is unhealthy. `debtPositionOf` calls `debtOf`, which iterates all of the account’s debt tokens and calls `IMasterOracle.quoteTokenToUsd` for each one. It also calls `depositOf`, which iterates all of the account’s deposit tokens and calls `quoteTokenToUsd` for each underlying asset. [1](#0-0) [2](#0-1) [3](#0-2) 

After the health check, `liquidate` can make another oracle query for `debtFloorInUsd` and calls `quoteLiquidateOut`. `quoteLiquidateOut` calls `masterOracle().quote` to convert the synthetic repayment amount into the selected collateral’s underlying token. There is no `try/catch`, cached quote, fallback oracle, or liquidation path that avoids these calls. [4](#0-3) [5](#0-4) [6](#0-5) 

Consequently, a failed or zero-valued quote for a held debt token, any held collateral token, the selected collateral underlying, or the repaid synthetic token can prevent liquidation even though `liquidate` is public and the liquidator is otherwise willing to repay the debt. A zero-priced collateral is especially problematic: its contribution to collateral value is zero, while conversion into that collateral can still revert or produce an unusable liquidation quote. [7](#0-6) 

### Impact Explanation
Liquidations can be unavailable during the exact market conditions where they are most necessary. If collateral becomes worthless or a required price feed becomes unavailable, the protocol cannot reduce the victim’s debt by seizing the affected collateral through `Pool.liquidate`. Debt continues accruing while collateral value deteriorates, leaving bad debt and potentially causing protocol insolvency. This is a liveness and solvency failure rather than merely a failed view function because the same oracle path is required before debt and collateral state are mutated. [8](#0-7) [7](#0-6) 

### Likelihood Explanation
The triggering condition is an external oracle outage, stale quote, reverting adapter, or legitimate collapse to a zero price. It does not require a privileged protocol action or incorrect oracle data. An unprivileged liquidator cannot prevent the condition, and every attempted liquidation that traverses the missing quote will revert. Positions holding multiple assets are additionally exposed because health evaluation prices every debt and deposit token in the account’s token lists, not only the synthetic token and collateral selected for liquidation. [1](#0-0) [3](#0-2) 

### Recommendation
Introduce a bounded degraded-oracle liquidation path. At minimum, isolate per-asset quote failures, support redundant or last-known-good prices with strict bounds, and allow liquidation from collateral whose price is available without requiring unrelated zero-valued collateral to be priced successfully. Explicitly define handling for zero-priced collateral so it cannot make an account immune to liquidation. Emergency behavior should limit liquidation size, incentives, and oracle-price deviation rather than trusting arbitrary replacement prices.

### Proof of Concept
A Foundry fork test can reproduce the liveness failure by arranging an unhealthy account and replacing the configured oracle’s runtime code with a reverting oracle. The public `liquidate` call reverts before burning the liquidator’s synthetic token, burning the victim’s debt token, or seizing collateral.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";
import {Pool} from "../contracts/Pool.sol";
import {IPoolRegistry} from "../contracts/interfaces/IPoolRegistry.sol";
import {IMasterOracle} from "../contracts/interfaces/external/IMasterOracle.sol";
import {IDepositToken} from "../contracts/interfaces/IDepositToken.sol";
import {ISyntheticToken} from "../contracts/interfaces/ISyntheticToken.sol";

contract AlwaysRevertingOracle is IMasterOracle {
    function quoteTokenToUsd(address, uint256) external pure returns (uint256) {
        revert("oracle offline");
    }

    function quoteUsdToToken(address, uint256) external pure returns (uint256) {
        revert("oracle offline");
    }

    function quote(address, address, uint256) external pure returns (uint256) {
        revert("oracle offline");
    }
}

contract OracleOutageLiquidationTest is Test {
    function test_liquidationRevertsWhenOracleIsUnavailable() external {
        // Fork an environment containing a deployed Pool, an unhealthy victim,
        // a registered debt synthetic token, and a registered deposit token.
        Pool pool = Pool(vm.envAddress("POOL"));
        IPoolRegistry registry = pool.poolRegistry();
        IMasterOracle oracle = registry.masterOracle();

        address victim = vm.envAddress("UNHEALTHY_ACCOUNT");
        address liquidator = vm.envAddress("LIQUIDATOR");
        ISyntheticToken synthetic = ISyntheticToken(vm.envAddress("SYNTHETIC"));
        IDepositToken collateral = IDepositToken(vm.envAddress("DEPOSIT_TOKEN"));
        uint256 amountToRepay = vm.envUint("AMOUNT_TO_REPAY");

        (bool unhealthyBefore,,,,) = pool.debtPositionOf(victim);
        assertFalse(unhealthyBefore);

        AlwaysRevertingOracle offline = new AlwaysRevertingOracle();
        vm.etch(address(oracle), address(offline).code);

        vm.prank(liquidator);
        vm.expectRevert("oracle offline");
        pool.liquidate(synthetic, victim, amountToRepay, collateral);
    }
}
```

The revert occurs inside `liquidate` at `debtPositionOf(account_)`: `debtOf` or `depositOf` invokes `quoteTokenToUsd`, and the exception propagates to the caller. If all account-level health quotes remain available but the selected collateral’s price is zero or its conversion fails, execution instead reaches `quoteLiquidateOut` and reverts while calculating `_totalToSeize`. [4](#0-3) [5](#0-4)

### Citations

**File:** contracts/Pool.sol (L156-179)
```text
    }

    /**
     * @dev Throws if synthetic token doesn't exist
     */
    modifier onlyIfSyntheticTokenExists(ISyntheticToken syntheticToken_) {
        if (!doesSyntheticTokenExist(syntheticToken_)) revert SyntheticDoesNotExist();
        _;
    }

    constructor() {
        _disableInitializers();
    }

    /// @inheritdoc Context
    function _msgSender() internal view virtual override(Context, SynthContext) returns (address) {
        return SynthContext._msgSender();
    }

    function initialize(IPoolRegistry poolRegistry_) public initializer {
        if (address(poolRegistry_) == address(0)) revert PoolRegistryIsNull();
        __Pauseable_init();

        _poolRegistry = poolRegistry_;
```

**File:** contracts/Pool.sol (L227-236)
```text
    function debtOf(address account_) public view override returns (uint256 _debtInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = debtTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDebtToken _debtToken = IDebtToken(debtTokensOfAccount.at(account_, i));
            _debtInUsd += _masterOracle.quoteTokenToUsd(
                address(_debtToken.syntheticToken()),
                _debtToken.balanceOf(account_)
            );
        }
```

**File:** contracts/Pool.sol (L248-265)
```text
    function debtPositionOf(
        address account_
    )
        public
        view
        override
        returns (
            bool _isHealthy,
            uint256 _depositInUsd,
            uint256 _debtInUsd,
            uint256 _issuableLimitInUsd,
            uint256 _issuableInUsd
        )
    {
        _debtInUsd = debtOf(account_);
        (_depositInUsd, _issuableLimitInUsd) = depositOf(account_);
        _isHealthy = _debtInUsd <= _issuableLimitInUsd;
        _issuableInUsd = _debtInUsd < _issuableLimitInUsd ? _issuableLimitInUsd - _debtInUsd : 0;
```

**File:** contracts/Pool.sol (L274-287)
```text
    function depositOf(
        address account_
    ) public view override returns (uint256 _depositInUsd, uint256 _issuableLimitInUsd) {
        IMasterOracle _masterOracle = masterOracle();
        uint256 _length = depositTokensOfAccount.length(account_);
        for (uint256 i; i < _length; ++i) {
            IDepositToken _depositToken = IDepositToken(depositTokensOfAccount.at(account_, i));
            uint256 _amountInUsd = _masterOracle.quoteTokenToUsd(
                address(_depositToken.underlying()),
                _depositToken.balanceOf(account_)
            );
            _depositInUsd += _amountInUsd;
            _issuableLimitInUsd += _amountInUsd.wadMul(_depositToken.collateralFactor());
        }
```

**File:** contracts/Pool.sol (L451-471)
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
```

**File:** contracts/Pool.sol (L537-589)
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
```

**File:** contracts/interfaces/external/IMasterOracle.sol (L5-10)
```text
interface IMasterOracle {
    function quoteTokenToUsd(address _asset, uint256 _amount) external view returns (uint256 _amountInUsd);

    function quoteUsdToToken(address _asset, uint256 _amountInUsd) external view returns (uint256 _amount);

    function quote(address _assetIn, address _assetOut, uint256 _amountIn) external view returns (uint256 _amountOut);
```
