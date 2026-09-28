### Title
`SmartFarmingManager::leverage()` and `flashRepay()` lack a `deadline` check — stale slippage parameters allow delayed execution at unfavorable prices - (File: contracts/SmartFarmingManager.sol)

### Summary
The bug class is a swap guarded only by a slippage bound, with no user-supplied `deadline`. In Metronome, both `SmartFarmingManager::leverage()` and `SmartFarmingManager::flashRepay()` route external swaps through `swapper().swapExactInput(...)` and validate the result only against user-provided min-out values (`depositAmountMin_`, `swapAmountOutMin_`) at execution time. Neither function accepts a deadline, so a transaction submitted by an unprivileged user can be executed long after submission — after oracle prices and pool ratios have moved — while still passing the stale slippage check.

### Finding Description
In `leverage()` (`contracts/SmartFarmingManager.sol:155`), the user supplies `depositAmountMin_` as the only execution-time protection:

- Step 1 swaps `tokenIn_` → collateral with `amountOutMin_ = 0` (`SmartFarmingManager.sol:184`).
- Step 2 computes `_debtAmount` via `pool.masterOracle().quote(...)` at whatever oracle price exists **at execution time** (`SmartFarmingManager.sol:189`, `SmartFarmingManager.sol:234`).
- Step 3 swaps minted synth → collateral again with `amountOutMin_ = 0`, then checks `if (_depositAmount < depositAmountMin_) revert LeverageSlippageTooHigh()` (`SmartFarmingManager.sol:197-198`).

`flashRepay()` (`SmartFarmingManager.sol:98`) is analogous: the collateral→synth swap at `SmartFarmingManager.sol:125` uses `amountOutMin_ = 0` and is only checked afterwards against `swapAmountOutMin_` (`SmartFarmingManager.sol:126`).

Neither signature includes a `deadline_` parameter, and no `block.timestamp` bound exists anywhere in the contract. The slippage bound is evaluated against the swap result at execution time, exactly like the report's `BadDebtProcessor::uniswapV3FlashCallback()` — a `swapAmountOutMin_`/`depositAmountMin_` chosen at submission time becomes stale if the mempool transaction is delayed while the oracle/market price drifts. The execution-time leverage economics (debt minted, effective leverage, refund amounts) all derive from the moved price, so the user ends up with a position or repayment outcome materially different from intent, within a slippage bound that no longer reflects the market the user priced it against.

### Impact Explanation
An unprivileged user's `leverage()` or `flashRepay()` transaction delayed in the mempool (congestion, low gas bid) executes under stale assumptions. For `leverage()`, the minted debt scales with the execution-time oracle quote while `depositAmountMin_` was priced against the submission-time quote; the user can receive a position with worse effective leverage and higher debt-to-collateral than intended, only bounded by `PositionIsNotHealthy` — a solvency floor, not the user's expectation. For `flashRepay()`, a moved collateral/synth price means the withdrawn collateral buys less synth, so less debt is repaid than planned, yet the stale `swapAmountOutMin_` still passes. This is a direct loss/suboptimal-execution-of-user-funds condition caused solely by missing temporal validity, matching the audited medium-severity class.

### Likelihood Explanation
Requires only normal mempool delay plus price drift — no privileged role, no oracle manipulation needed (the report's own class relies on legitimate price movement between submission and execution). Any user submitting these calls with tight slippage and a low gas price is exposed. Likelihood is moderate: it is environmental rather than attacker-triggered, consistent with a Medium finding.

### Recommendation
Add a `deadline_` parameter to `leverage()` and `flashRepay()` (and to `_swap`/`swapExactInput` if the Swapper supports it), reverting when `block.timestamp > deadline_`. Users should set the deadline at submission so stale transactions revert instead of executing at moved prices.

### Proof of Concept
Conceptual Foundry fork test (mainnet fork, deployed `SmartFarmingManager`):

```solidity
// contracts/SmartFarmingManager.sol — leverage() has no deadline param
function testLeverageStaleSlippage() public {
    // 1. user computes depositAmountMin_ against current oracle quote
    uint256 quoteNow = pool.masterOracle().quote(collateral, synth, amount);
    uint256 depositMin = quoteNow * 99 / 100; // priced at submission-time quote

    // 2. tx sits in mempool; warp forward and move the oracle/swapper price
    vm.warp(block.timestamp + 1 hours);
    _movePriceDown(synth, 20_00); // synth price moves 20% — legitimate drift

    // 3. leverage() still executes — no deadline check exists
    vm.prank(user);
    sfm.leverage(tokenIn, depositToken, synthToken, amountIn, 2e18, depositMin);
    // slippage check passes on stale depositMin, but minted debt and
    // deposit are computed at the moved execution-time quote → user
    // receives a position worse than priced, with no way to have
    // bounded execution time.
}
```

The key observation is that `leverage()`/`flashRepay()` signatures and `_swap()` (`SmartFarmingManager.sol:293-311`) contain no `block.timestamp` validation, so step 3 cannot revert on elapsed time.

Note: because the slippage bounds are absolute min-outs, the strict worst-case loss is bounded by the user's own min-out parameter; the vulnerability is that the *intended* execution economics (leverage ratio, repaid amount, minted debt) can be stale without any temporal guard — the same impact profile accepted as Medium in the referenced report. I was not able to inspect the deployed `Swapper` implementation to confirm whether `swapExactInput` itself enforces a deadline internally; if it does, the practical exposure narrows to the oracle-priced debt computation in `leverage()`.