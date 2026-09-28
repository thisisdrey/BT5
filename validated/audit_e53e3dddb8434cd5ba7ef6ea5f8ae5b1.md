### Title
Missing deadline checks in `SmartFarmingManager.leverage` and `flashRepay` allow stale pending transactions to execute as bad trades - (File: contracts/SmartFarmingManager.sol)

### Summary
`SmartFarmingManager.leverage()` and `SmartFarmingManager.flashRepay()` let users bound execution only via minimum-output slippage parameters (`depositAmountMin_`, `swapAmountOutMin_`), but expose no `deadline` parameter. A transaction signed with a slippage bound that was reasonable at signing time can remain pending in the mempool and be mined much later, when market prices — and therefore the relevance of that stale slippage bound — have changed drastically. The user ends up executing a leverage/deleverage at a price they would never have accepted, or gets sandwiched by an MEV bot exploiting the now-outdated slippage tolerance.

### Finding Description
Both public entry points route user funds through external AMM swaps via `swapper().swapExactInput(...)`:

- `leverage()` performs up to two AMM swaps (`tokenIn_ -> collateral` and `syntheticToken -> collateral`), each with `amountOutMin_ = 0`, and the only protection is a single `depositAmountMin_` check on the aggregate result [1](#0-0) .
- `flashRepay()` withdraws collateral and swaps it for synthetic debt token, protected only by `swapAmountOutMin_` [2](#0-1) .

Neither signature accepts a `deadline`, and `grep` shows no deadline usage anywhere in production contracts — only in vendored stargate dependencies [3](#0-2) [4](#0-3) .

Attack scenario matching the reference report:

1. Alice calls `leverage(WETH, depositToken, msETH, amountIn, 3e18, depositAmountMin)` with a low gas price. The tx stays pending.
2. Collateral price moves significantly. Alice's `depositAmountMin_` was computed against the old price; it is now satisfiable even after heavy sandwich slippage, or the trade simply executes at a much worse effective entry price than intended.
3. A MEV bot sandwiches the `syntheticToken -> collateral` swap (or the delayed tx simply mines at the worse price). Alice's leveraged position is opened at bad terms — she holds more debt relative to deposited collateral than she agreed to — and the `PositionIsNotHealthy` check still passes because slippage losses shrink the deposit, not the collateral factor.

The same applies to `flashRepay()`: a pending tx mined late repays debt at a stale collateral→synth exchange rate, causing Alice to repay less debt than expected for the same withdrawn collateral.

`Pool.swap` is not a better analog — it is oracle-priced (`masterOracle.quote`), so execution price does not depend on when the tx mines; the AMM-routed paths in `SmartFarmingManager` are where real market-price slippage applies.

### Impact Explanation
Direct user loss: pending `leverage`/`flashRepay` transactions can be mined at prices far worse than intended, or be sandwiched once the user's stale slippage bound accommodates a large price deviation. Losses are bounded by the user-supplied min-amount parameters, but those parameters are exactly what becomes stale — identical to the Arrakis router finding.

### Likelihood Explanation
Any EOA can submit a low-fee tx that later mines at a bad price; no privileged role required. Exploitability depends on mempool congestion and volatility, which is common on mainnet. However, impact is limited to the caller's own funds (no protocol insolvency, no theft from other users), and the user did provide a min-amount bound, so severity is lower than a case with no protection at all — likely Medium rather than High.

### Recommendation
Add a `deadline_` parameter to `leverage()` and `flashRepay()` (and the cross-chain retry/callback entry points if intended), reverting when `block.timestamp > deadline_`, as in Uniswap V2 routers.

### Proof of Concept
A Foundry fork test: (1) user signs `leverage(...)` calldata with `depositAmountMin_` computed from current prices; (2) advance time / move the `syntheticToken->collateral` pool price via a large swap so oracle-vs-AMM price diverges; (3) execute the stale calldata — it succeeds, producing a materially worse deposit than the user intended, or is sandwiched. No deadline check exists to revert it.

### Citations

**File:** contracts/SmartFarmingManager.sol (L98-103)
```text
    function flashRepay(
        ISyntheticToken syntheticToken_,
        IDepositToken depositToken_,
        uint256 withdrawAmount_,
        uint256 swapAmountOutMin_
    )
```

**File:** contracts/SmartFarmingManager.sol (L121-126)
```text
        // 1. withdraw collateral
        (_withdrawn, ) = depositToken_.flashWithdraw(_msgSender, withdrawAmount_);

        // 2. swap it for synth
        uint256 _swapAmountOut = _swap(swapper(), _collateralOf(depositToken_), _syntheticToken, _withdrawn, 0);
        if (_swapAmountOut < swapAmountOutMin_) revert FlashRepaySlippageTooHigh();
```

**File:** contracts/SmartFarmingManager.sol (L155-161)
```text
    function leverage(
        IERC20 tokenIn_,
        IDepositToken depositToken_,
        ISyntheticToken syntheticToken_,
        uint256 amountIn_,
        uint256 leverage_,
        uint256 depositAmountMin_
```

**File:** contracts/SmartFarmingManager.sol (L182-198)
```text
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
```
