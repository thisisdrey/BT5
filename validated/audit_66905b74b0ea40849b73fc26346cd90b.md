### Title
`Pool.swap` offers no slippage protection — users cannot specify a minimum amount out on oracle-priced synthetic swaps - (File: contracts/Pool.sol)

### Summary
`Pool.swap` exchanges one synthetic asset for another at a price derived entirely from `MasterOracle.quote`, minus `FeeProvider.swapFees`. Its signature — `swap(ISyntheticToken syntheticTokenIn_, ISyntheticToken syntheticTokenOut_, uint256 amountIn_)` — has no `amountOutMin_` / `sqrtPriceLimit`-style parameter, so the caller has no way to bound the output. Whatever the oracle reports at execution time is what the user receives, identical in spirit to the `PerpDepository` issue where `sqrtPriceLimitX96` is hardcoded to `0` ("no limit"). By contrast, `SmartFarmingManager.leverage`/`flashRepay` do expose slippage parameters (`depositAmountMin_`, `swapAmountOutMin_`) checked against the final outcome [1](#0-0) [2](#0-1) , which shows the codebase recognizes the class but leaves `Pool.swap` unprotected.

### Finding Description
- `swap` is defined in `IPool` with only `(syntheticTokenIn_, syntheticTokenOut_, amountIn_)` — no min-out argument [3](#0-2) .
- The output is computed solely by `quoteSwapOut`, which calls `masterOracle().quote(tokenIn, tokenOut, amountIn_)` and subtracts `swapFees` [4](#0-3) . There is no user-supplied floor and no comparison against any expected value.
- The swap burns `syntheticTokenIn_` and mints `syntheticTokenOut_` to the caller at that oracle-derived rate. A user who quoted the swap off-chain (or via `quoteSwapOut`) has no on-chain guarantee: any oracle price movement — a scheduled feed update landing in the same block, or an attacker manipulating a spot-dependent oracle source (e.g., a vault-share/exchange-rate-priced asset moved via donation or AMM trade within the same transaction) — changes `amountOut_` arbitrarily against the user, and the transaction still succeeds.
- An attacker can therefore sandwich a victim's `swap` transaction: move the manipulable oracle source to inflate `syntheticTokenOut_`'s price before the victim's tx, let the victim receive fewer tokens than quoted, then restore the price and pocket the difference via a reverse swap. Same-transaction oracle manipulation is explicitly within scope.

### Impact Explanation
Direct loss of user funds: the victim's `syntheticTokenIn_` is burned at a worse effective rate than fairly quoted, with the extracted value capturable by the sandwiching attacker. Because synthetic tokens are minted/burned at oracle price rather than traded against an AMM, the "slippage" manifests as a mispriced mint — the user simply receives less `syntheticTokenOut_` than the fair quote, permanently.

### Likelihood Explanation
- `Pool.swap` is a public, unprivileged entry point gated only by `isSwapActive` / pause flags.
- Realization requires the oracle price to shift between the user's quote and execution — either through a normal feed update racing the user's tx or through an attacker moving a spot-manipulable oracle component. The former is a routine mempool-timing condition on chains with public mempools; the latter depends on whether any `MasterOracle` constituent feed is same-transaction manipulable, which is plausible for exchange-rate/vault-share priced assets but configuration-dependent.
- No modifier, cap, or guard compensates for the missing min-out check; the health check in `SmartFarmingManager` does not apply to direct `Pool.swap` calls.

### Recommendation
Add an `amountOutMin_` parameter to `Pool.swap` (and a symmetric `amountInMax_` if an exact-output variant is added), reverting when `_amountOut < amountOutMin_`, consistent with the slippage parameters already used in `SmartFarmingManager.leverage`, `flashRepay`, and the cross-chain retry functions.

### Proof of Concept
Conceptual Hardhat fork test (mirroring the pattern in `test/Pool.test.ts` and `test/SmartFarmingManager.test.ts` which already manipulate `SwapperMock`/`MasterOracleMock` rates to demonstrate slippage [5](#0-4) ):

```ts
// fork mainnet; alice holds msETH, wants msUSD
const quoted = await pool.quoteSwapOut(msEth.address, msUsd.address, amountIn)

// attacker (or a racing oracle update) moves the oracle price of msUSD up 5%
// in the same block / preceding tx — e.g., via donation to the rate source
// or a scheduled push oracle update landing first
await manipulateOracleSource() // same-tx spot manipulation of a feed component

// victim's tx executes at the worse price; nothing reverts
await pool.connect(alice).swap(msEth.address, msUsd.address, amountIn)
const received = await msUsd.balanceOf(alice.address)

// alice received ~5% less than quoted and had no param to prevent it
expect(received).to.be.lt(quoted.mul(95).div(100))
```

Using the mock harness, the same point is shown by calling `masterOracle.updatePrice(msUSD, higherPrice)` between `quoteSwapOut` and `swap` — the swap succeeds at the degraded rate because no `amountOutMin_` exists to check against.

### Citations

**File:** contracts/SmartFarmingManager.sol (L125-126)
```text
        uint256 _swapAmountOut = _swap(swapper(), _collateralOf(depositToken_), _syntheticToken, _withdrawn, 0);
        if (_swapAmountOut < swapAmountOutMin_) revert FlashRepaySlippageTooHigh();
```

**File:** contracts/SmartFarmingManager.sol (L196-198)
```text
            // 3. swap synth for collateral
            uint256 _depositAmount = amountIn_ + _swap(_swapper, syntheticToken_, _collateral, _issued, 0);
            if (_depositAmount < depositAmountMin_) revert LeverageSlippageTooHigh();
```

**File:** contracts/interfaces/IPool.sol (L100-104)
```text
    function swap(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    ) external returns (uint256 _amountOut, uint256 _fee);
```

**File:** contracts/Pool.sol (L508-524)
```text
    function quoteSwapOut(
        ISyntheticToken syntheticTokenIn_,
        ISyntheticToken syntheticTokenOut_,
        uint256 amountIn_
    ) public view override returns (uint256 _amountOut, uint256 _fee) {
        _amountOut = _poolRegistry.masterOracle().quote(
            address(syntheticTokenIn_),
            address(syntheticTokenOut_),
            amountIn_
        );

        uint256 _swapFee = feeProvider.swapFees(address(syntheticTokenIn_), address(syntheticTokenOut_));

        if (_swapFee > 0) {
            _fee = _amountOut.wadMul(_swapFee);
            _amountOut -= _fee;
        }
```

**File:** test/SmartFarmingManager.test.ts (L256-268)
```typescript
    it('should revert if slippage is too high', async function () {
      // given
      await swapper.updateRate(parseEther('0.9')) // 10% slippage

      // when
      const withdrawAmount = parseEther('50')
      const repayAmountMin = parseEther('49.5') // 1% slippage
      const tx = smartFarmingManager
        .connect(alice)
        .flashRepay(msUSD.address, msdVaDAI.address, withdrawAmount, repayAmountMin)

      // then
      await expect(tx).revertedWithCustomError(smartFarmingManager, 'FlashRepaySlippageTooHigh')
```
