### Title
Missing slippage protection in `Pool.swap()` — users cannot bound `amountOut`, so oracle quote changes between signature and execution silently execute unfavorable swaps — (File: contracts/Pool.sol)

### Summary
`Pool.swap()` burns `amountIn_` of `syntheticTokenIn_` and mints an `amountOut` of `syntheticTokenOut_` computed at execution time from `quoteSwapOut`, which relies entirely on `masterOracle().quote()`. The function takes no `minAmountOut` (or expected-price/deadline) parameter, so the user has no way to constrain the output. Any unfavorable oracle price movement — including a front-run AMM trade that moves a DEX-derived feed consumed by `MasterOracle` in the same block — changes the result while the transaction still succeeds. This is the same defect class as `wakeSleepers()` with no sleeper-index argument: the user signs a transaction whose outcome is whatever the mutable execution context happens to be, not what they approved.

### Finding Description
In `Pool.swap` (contracts/Pool.sol:642-671):

```solidity
syntheticTokenIn_.burn(_msgSender, amountIn_);

(_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);

if (_fee > 0) {
    syntheticTokenOut_.mint(_poolRegistry.feeCollector(), _fee);
}
syntheticTokenOut_.mint(_msgSender, _amountOut);
```

`quoteSwapOut` (contracts/Pool.sol:508-525) prices the swap via `masterOracle().quote(tokenIn, tokenOut, amountIn)` and subtracts the fee. The input tokens are burned *before* the quote is even read, and there is no check that `_amountOut` meets any user expectation. Reaching this path requires only: swap active (`isSwapActive` defaults true in `initialize`), both synthetics registered, and the caller holding `amountIn_` balance — all public, unprivileged conditions. `whenNotShutdown`/`nonReentrant` do not prevent front-running or price drift between mempool observation and inclusion.

The relevant oracle is external (`IMasterOracle`, contracts/interfaces/external/IMasterOracle.sol), so the magnitude of exploitable slippage depends on the deployed feeds. Where any constituent price is sourced from a manipulable venue (AMM spot/short-TWAP, or a vault-share priced collateral), an unprivileged attacker can move the quote before the victim's swap lands — e.g., push `syntheticTokenIn`'s price down so the victim receives less `syntheticTokenOut`, then restore the price. Even absent active manipulation, a stale-signed tx following a legitimate oracle update executes at the new, worse price with no recourse.

### Impact Explanation
Users lose value on swaps: tokens are irrevocably burned and a worse-than-expected amount is minted, with the difference accruing to the system/front-runner rather than reverting. This is direct loss of user funds proportional to the price deviation, uncapped by any user-supplied bound. Impact scales with swap size and oracle price volatility/manipulability.

### Likelihood Explanation
Medium. Front-running requires either a manipulable price source in `MasterOracle` or adverse price updates landing between signing and inclusion — both are realistic on-chain conditions, and the absence of any `minAmountOut` means every swap is exposed by design. The function is public and gated only by token-existence and balance checks.

### Recommendation
Add a `minAmountOut_` parameter (and optionally a deadline) to `swap`, reverting with an explicit error if `quoteSwapOut` returns less:

```solidity
function swap(
    ISyntheticToken syntheticTokenIn_,
    ISyntheticToken syntheticTokenOut_,
    uint256 amountIn_,
    uint256 minAmountOut_
) external ... {
    ...
    (_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);
    if (_amountOut < minAmountOut_) revert SlippageExceeded();
    ...
}
```

Alternatively perform the oracle quote *before* burning `amountIn_` so a revert leaves the user's balance untouched (currently the burn happens first, though a revert would roll it back anyway).

### Proof of Concept
Foundry fork test sketch against a deployed Pool:

```solidity
// setup: pool, msUSD (in), msETH (out), victim holds amountIn of msUSD
uint256 amountIn = 1_000e18;

// 1. Victim observes quote: expectOut = pool.quoteSwapOut(msUSD, msETH, amountIn)
//    and signs swap(msUSD, msETH, amountIn) — no way to specify minOut.

// 2. Attacker front-runs: manipulates the underlying DEX pool feeding
//    MasterOracle for msUSD (or a vault-share collateral quote),
//    dropping the effective tokenIn price by X%.

// 3. Victim tx lands:
pool.swap(msUSD, msETH, amountIn);

// assert: amountOut == expectedOut * (1 - X%)
// No revert occurred; victim's msUSD was burned and they received
// materially less msETH than the quote they signed against.
```

The test demonstrates that `swap` succeeds at any oracle-determined output, and that adding `minAmountOut` would have reverted the transaction, bounding the loss.