### Title
Missing `deadline` param in `SmartFarmingManager.leverage()` and `flashRepay()` swaps allows delayed mempool transactions to execute at stale slippage bounds - (File: contracts/SmartFarmingManager.sol)

### Summary
`SmartFarmingManager.leverage()` and `SmartFarmingManager.flashRepay()` perform user-initiated swaps through the external `Swapper` contract via `ISwapper.swapExactInput()`. These calls enforce a slippage bound (`depositAmountMin_` / `swapAmountOutMin_`), but neither the functions nor the underlying `ISwapper` interface accept a `deadline` parameter. A user's transaction can sit pending in the mempool and be included by a validator much later than intended, when market prices have moved against them — the stale `amountOutMin` still allows execution at a worse effective price, and the user loses any positive slippage they would have received at the intended execution time. This is the same bug class as the reference report (slippage enforced, deadline missing), and in Metronome it is reachable by any unprivileged user through public entry points.

### Finding Description
In `leverage()`, the user provides `depositAmountMin_` as the only execution-protection parameter. The function swaps `tokenIn_ -> collateral` (line 184) and `syntheticToken_ -> collateral` (line 197) via `_swap()`, which calls `swapper_.swapExactInput(tokenIn, tokenOut, amountIn, amountOutMin, to)` — note `amountOutMin_` is passed as `0` at line 184 and the effective check happens later against `depositAmountMin_` (line 198). [1](#0-0) 

In `flashRepay()`, the user provides `swapAmountOutMin_`; the withdrawn collateral is swapped to the synthetic token via `_swap(swapper(), _collateral, _syntheticToken, _withdrawn, 0)` with the check deferred to line 126. [2](#0-1) 

The internal `_swap` forwards to `ISwapper.swapExactInput`, whose interface (`contracts/interfaces/external/ISwapper.sol:6-12`) has no `deadline` parameter — the deployed Swapper therefore has no way to bound execution time even if it wanted to. [3](#0-2) 

Unlike `Pool.swap` (which is oracle-priced and deterministic), these SmartFarmingManager swaps route through an external DEX aggregator (`Swapper`), so the realized price depends on the market state at inclusion time, not at signing time.

### Impact Explanation
A user's `leverage()` or `flashRepay()` transaction can be held in the mempool by a PoS block proposer (proposers are known 6–12 minutes ahead) and included later when the collateral/synth exchange rate has moved. Because the only protection is `amountOutMin` evaluated at inclusion time:

- If the price moved against the user but stays above the stale min bound, the tx executes at a worse price than the user intended — guaranteed loss of positive slippage on potentially large leveraged amounts (leverage multiplies the swapped notional, amplifying the loss).
- If the price moved below the bound, the tx reverts, wasting the user's gas and leaving their position unchanged.

The loss is borne entirely by the unprivileged user; no privileged role is required to trigger or exploit the condition — it is a property of delegated block inclusion.

### Likelihood Explanation
Likelihood is moderate: it requires the tx to be delayed in the public mempool (or via a builder/validator holding it) and the market rate to drift unfavorably before inclusion — the same conditions that justified Medium severity in the reference report. Leverage swaps are large (debt-minted amounts are swapped too, line 197), so absolute slippage losses are correspondingly large, while the fix cost (adding a deadline param) is minimal. Note the invariant broken is not solvency but fair execution price — the damage is user-level loss of expected output, not protocol insolvency.

### Recommendation
Add a `deadline_` parameter to `leverage()` and `flashRepay()` (and to the cross-chain variants in `CrossChainDispatcher`, which are even more exposed since tx2/tx3 execute on another chain at an unknown later time), propagate it into `ISwapper.swapExactInput`, and revert if `block.timestamp > deadline_` at execution time. Never use `block.timestamp` itself as the deadline inside the swap call.

### Proof of Concept
Hardhat fork scenario:

```ts
// test/DeadlineSwap.test.ts (sketch against forked deployment)
// 1. Alice builds leverage tx with depositAmountMin calibrated to current prices
const tx = await smartFarmingManager.connect(alice).populateTransaction.leverage(
  vaDAI.address, msdVaDAI.address, msUSD.address,
  amountIn, leverage, depositAmountMin
);

// 2. Simulate mempool delay: mine / warp forward (e.g. evm_increaseTime 10 min)
//    while the DEX pool price of synth->collateral drops 4% (still above depositAmountMin)

// 3. Include the tx now. Observe: tx succeeds, but alice's final deposit
//    is ~4% lower than the quoted amount at signing time.
//    There is no deadline param to make the tx revert, so the loss is realized.
//    With a deadline param, step 3 would revert and alice keeps her funds.
```

Concretely, the same fork test used in `test/SmartFarmingManager.test.ts:356-370` (which manipulates `swapper.updateRate` to simulate price movement) demonstrates the loss path: if the rate change happens *after* the user signs but *before* inclusion, the user cannot abort — there is no deadline to enforce it. [4](#0-3)

### Citations

**File:** contracts/SmartFarmingManager.sol (L98-144)
```text
    function flashRepay(
        ISyntheticToken syntheticToken_,
        IDepositToken depositToken_,
        uint256 withdrawAmount_,
        uint256 swapAmountOutMin_
    )
        external
        override
        whenNotShutdown
        nonReentrant
        onlyIfDepositTokenExists(depositToken_)
        onlyIfSyntheticTokenExists(syntheticToken_)
        returns (uint256 _withdrawn, uint256 _repaid)
    {
        ISyntheticToken _syntheticToken = syntheticToken_; // stack too deep

        address _msgSender = _msgSender();
        if (withdrawAmount_ == 0) revert AmountIsZero();
        if (withdrawAmount_ > depositToken_.balanceOf(_msgSender)) revert AmountIsTooHigh();
        IPool _pool = pool;
        IDebtToken _debtToken = _pool.debtTokenOf(_syntheticToken);
        if (swapAmountOutMin_ > _debtToken.balanceOf(_msgSender)) revert AmountIsTooHigh();

        // 1. withdraw collateral
        (_withdrawn, ) = depositToken_.flashWithdraw(_msgSender, withdrawAmount_);

        // 2. swap it for synth
        uint256 _swapAmountOut = _swap(swapper(), _collateralOf(depositToken_), _syntheticToken, _withdrawn, 0);
        if (_swapAmountOut < swapAmountOutMin_) revert FlashRepaySlippageTooHigh();

        (uint256 _maxRepayAmount, ) = _debtToken.quoteRepayIn(_debtToken.balanceOf(_msgSender));
        uint256 _amountToRepay = Math.min(_swapAmountOut, _maxRepayAmount);

        // 3. repay debt
        (_repaid, ) = _debtToken.repay(_msgSender, _amountToRepay);

        // 4. refund synthetic token in excess
        if (_swapAmountOut > _amountToRepay) {
            _syntheticToken.safeTransfer(_msgSender, _swapAmountOut - _amountToRepay);
        }

        // 5. check the health of the outcome position
        (bool _isHealthy, , , , ) = _pool.debtPositionOf(_msgSender);
        if (!_isHealthy) revert PositionIsNotHealthy();

        emit FlashRepaid(_syntheticToken, depositToken_, _withdrawn, _repaid);
    }
```

**File:** contracts/SmartFarmingManager.sol (L155-211)
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
    {
        if (amountIn_ == 0) revert AmountIsZero();
        if (leverage_ <= 1e18) revert LeverageTooLow();
        if (leverage_ > uint256(1e18).wadDiv(1e18 - depositToken_.collateralFactor())) revert LeverageTooHigh();

        address _msgSender = _msgSender();
        ISwapper _swapper = swapper();

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
    }
```

**File:** contracts/SmartFarmingManager.sol (L273-311)
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
    }
```

**File:** contracts/interfaces/external/ISwapper.sol (L5-12)
```text
interface ISwapper {
    function swapExactInput(
        address tokenIn_,
        address tokenOut_,
        uint256 amountIn_,
        uint256 amountOutMin_,
        address receiver_
    ) external returns (uint256 _amountOut);
```
