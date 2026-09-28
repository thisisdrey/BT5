### Title
`Pool.swap` lacks a `minAmountOut` slippage check, exposing users to oracle price movement between submission and execution - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool.swap` burns the caller's `syntheticTokenIn_` and mints `syntheticTokenOut_` priced purely by `masterOracle().quote(...)` at execution time, with no `minAmountOut_` parameter. The function signature is `swap(ISyntheticToken, ISyntheticToken, uint256)` — the user commits `amountIn_` but cannot bound `amountOut_`. If the oracle price moves against the user while the transaction sits in the mempool (or is reordered), the swap executes at the worse price with no revert path.

### Finding Description
In `Pool.swap` (contracts/Pool.sol:642-671), the flow is:

1. `syntheticTokenIn_.burn(_msgSender, amountIn_)` — user's input is burned unconditionally (line 660).
2. `(_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_)` — output quoted at execution-time oracle price (line 662).
3. `syntheticTokenOut_.mint(_msgSender, _amountOut)` — minted regardless of how unfavorable the quote is (line 668).

`quoteSwapOut` (lines 508-525) delegates entirely to `masterOracle().quote(tokenIn, tokenOut, amountIn_)` and applies `feeProvider.swapFees`. There is no caller-supplied minimum, no deadline, and no comparison against any expected amount. The only guards are `whenNotShutdown`, `nonReentrant`, `onlyIfSyntheticTokenExists`, and `isSwapActive` — none of which bound execution price.

Contrast with `SmartFarmingManager.flashRepay` and `leverage` (contracts/SmartFarmingManager.sol:98-144, 155-210), which do accept `swapAmountOutMin_`/`depositAmountMin_` and revert with `FlashRepaySlippageTooHigh`/`LeverageSlippageTooHigh` — the protocol applies slippage protection to external `Swapper` trades but not to the oracle-priced `Pool.swap`, even though oracle prices can equally move between mempool and execution.

The invariant broken is value-conservation of the user's swap intent: a user who observed quote `Q` at submission may receive `Q' < Q` arbitrarily lower (bounded only by oracle price movement), losing the difference with no recourse.

### Impact Explanation
Direct loss of user funds. A user swapping e.g. msETH→msUSD who submits based on `quoteSwapOut` at price P, but whose transaction executes after ETH's oracle price drops, receives proportionally less msUSD. The burned msETH is gone and the swap cannot revert. For large swaps or volatile windows (oracle updates, market moves), the loss is unbounded relative to the user's expectation. An attacker can also front-run a pending oracle update (or, where a quote source is manipulable within the same transaction, sandwich the swap) to worsen the victim's execution.

### Likelihood Explanation
Medium. The path is a plain public external call reachable by any EOA holding a synthetic token; no privileged role is needed. It only materializes a loss when the oracle price moves between submission and execution — a common mempool-delay/reordering scenario — or when an oracle update is front-runnable. It does not occur under fully static prices.

### Recommendation
Add a `minAmountOut_` parameter to `IPool.swap`/`Pool.swap` and revert if `quoteSwapOut`'s result is below it, e.g.:

```solidity
function swap(
    ISyntheticToken syntheticTokenIn_,
    ISyntheticToken syntheticTokenOut_,
    uint256 amountIn_,
    uint256 minAmountOut_
) ... {
    ...
    (_amountOut, _fee) = quoteSwapOut(syntheticTokenIn_, syntheticTokenOut_, amountIn_);
    if (_amountOut < minAmountOut_) revert SlippageTooHigh();
    syntheticTokenIn_.burn(_msgSender, amountIn_); // burn only after checks
    ...
}
```

Also consider burning `syntheticTokenIn_` after the slippage check and adding a `deadline` parameter for defense-in-depth.

### Proof of Concept
Hardhat-style sketch against the existing test setup (see `test/Pool.test.ts:1067-1125`):

```ts
// alice holds msETH; quoted msUSD out at ETH price P0
const amountIn = await msETH.balanceOf(alice.address)
const {_amountOut: quoted} = await pool.quoteSwapOut(msETH.address, msUSD.address, amountIn)

// oracle price of ETH drops 10% before tx executes (e.g., mempool delay / front-run update)
await ethOracle.updatePrice(ethPrice.mul(90).div(100))

// swap executes anyway; no minAmountOut exists to stop it
const tx = await pool.connect(alice).swap(msETH.address, msUSD.address, amountIn)
const received = (await msUSD.balanceOf(alice.address))

// received == quoted * 0.9, alice has no way to revert — confirms missing slippage check
expect(received).to.eq(quoted.mul(90).div(100))
```

The same pattern is demonstrated by `SmartFarmingManager`'s own tests (`test/SmartFarmingManager.test.ts:256-269`), where a degraded swap rate reverts only because a `swapAmountOutMin_` parameter exists — `Pool.swap` has no equivalent parameter.