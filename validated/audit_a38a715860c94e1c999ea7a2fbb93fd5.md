### Title
Pool admin can frontrun swaps by instantly raising per-bin additional fees or notional fees with no timelock — (`metric-core/contracts/MetricOmmPoolFactory.sol`)

### Summary
`setPoolBinAdditionalFees` and `setPoolAdminFees` allow the pool admin to change fees that are consumed live during `swap()` execution, with no timelock, no cooldown, and no slippage guard. A malicious pool admin can frontrun a pending swap by raising `addFeeBuyE6`/`addFeeSellE6` (up to `uint16.max` = 6.5535% in E6 units) or `adminNotionalFeeE8` (up to 1%), causing the trader to pay substantially more than the price they observed.

### Finding Description

The `MetricOmmPoolFactory` exposes two fee-change paths for the pool admin with no delay:

**Path 1 — `setPoolBinAdditionalFees`:** [1](#0-0) 

This instantly writes new per-bin fees to `_binStates[bin]` with no timelock, no cooldown, and no `collectFees` call. The `addFeeBuyE6` parameter is typed as `uint16`, so the pool admin can set it to `65535` (6.5535% in E6 units) in a single transaction.

**Path 2 — `setPoolAdminFees`:** [2](#0-1) 

This updates `notionalFeeE8` in the pool (after collecting existing fees at the old rate), but does not prevent frontrunning of future swaps.

Both fees are consumed live during `swap()`. The per-bin fees are added to `baseFeeX64` in every bin step: [3](#0-2) 

The notional fee is multiplied against the output amount, reducing what the trader receives: [4](#0-3) 

The effective buy/sell prices as seen in `getSellAndBuyPrices` confirm both fees directly widen the spread the trader pays: [5](#0-4) 

**The protocol already has a timelock for price provider changes** (`priceProviderTimelock[pool]`, enforced in `proposePoolPriceProvider` / `executePoolPriceProviderUpdate`): [6](#0-5) 

And the oracle's `confidenceParam` has a 1-minute `CONFIDENCE_COOLDOWN`: [7](#0-6) 

Fee changes have no analogous protection. The asymmetry is the root cause.

**Attack flow:**
1. Pool admin observes a pending `swap()` in the mempool.
2. Pool admin submits `setPoolBinAdditionalFees(pool, activeBin, 65535, 0)` with higher gas, frontrunning the swap.
3. The trader's swap executes with `addFeeBuyE6 = 65535` (6.5535%) added to `baseFeeX64`.
4. The trader pays ~6.55% more token1 than the price they observed, with no recourse.

The pool admin role is set by the permissionless `createPool` caller — it is not the trusted factory owner or oracle admin, so it is not excluded by the "trusted admin" out-of-scope rule. A malicious actor can deploy a pool, attract volume, then execute this attack.

### Impact Explanation

Direct loss of trader principal. The trader receives less output (or pays more input) than the price they observed before submitting the transaction. The maximum extractable fee per swap is up to 6.5535% of the swap notional via `addFeeBuyE6`, plus up to 1% via `adminNotionalFeeE8` — totaling ~7.5% of the swap value per transaction, well above Sherlock's Medium threshold for direct principal loss.

### Likelihood Explanation

Medium. The pool admin role is assigned by the permissionless `createPool` caller: [8](#0-7) 

Any actor can deploy a pool, set themselves as admin, attract liquidity and volume, then execute the frontrun. The attack requires only mempool visibility (standard on most EVM chains) and a single transaction with higher gas.

### Recommendation

1. **Add a timelock for `setPoolBinAdditionalFees` and `setPoolAdminFees`**, analogous to `priceProviderTimelock[pool]`. Propose the change, enforce a delay, then execute — identical to the price provider update pattern already in the factory.
2. **Alternatively, add a `maxFee` parameter to `swap()`** so traders can specify the maximum acceptable total fee (spread + notional + bin additional), reverting if the actual fee exceeds it. This is the direct analog to the Teller Finance recommendation: allow the caller to specify the exact fees they were expecting.

### Proof of Concept

```
Setup:
  Pool deployed with addFeeBuyE6 = 0 for bin 0, notionalFeeE8 = 0.
  Alice submits swap(zeroForOne=false, amountSpecified=1000e18, ...)
    — expects to buy token0 at mid price + baseFee only.

Attack (same block, higher gas):
  Pool admin calls setPoolBinAdditionalFees(pool, 0, 65535, 0)
    → _binStates[0].addFeeBuyE6 = 65535 (6.5535% in E6)

Execution (Alice's swap runs after):
  effectiveBuyFee = baseFeeX64 + mulDiv(65535, ONE_X64, 1e6)
                  ≈ baseFee + 6.55%
  Alice pays ~6.55% more token1 than expected for the same token0 output.
  The excess is captured as LP fee revenue controlled by the pool admin.
```

### Citations

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L212-213)
```text
    poolAdmin[pool] = params.admin;
    priceProviderTimelock[pool] = params.priceProviderTimelock;
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L408-435)
```text
  function setPoolAdminFees(address pool, uint24 newAdminSpreadFeeE6, uint24 newAdminNotionalFeeE8)
    external
    override
    nonReentrant
    onlyPoolAdmin(pool)
  {
    if (newAdminSpreadFeeE6 > maxAdminSpreadFeeE6) revert AdminFeeTooHigh();
    if (newAdminNotionalFeeE8 > maxAdminNotionalFeeE8) revert AdminFeeTooHigh();

    PoolFeeConfig memory c = poolFeeConfig[pool];
    IMetricOmmPoolCollectFees(pool)
      .collectFees(
        c.protocolSpreadFeeE6,
        c.adminSpreadFeeE6,
        c.protocolNotionalFeeE8,
        c.adminNotionalFeeE8,
        poolAdminFeeDestination[pool]
      );

    c.adminSpreadFeeE6 = newAdminSpreadFeeE6;
    c.adminNotionalFeeE8 = newAdminNotionalFeeE8;
    poolFeeConfig[pool] = c;

    IMetricOmmPoolFactoryActions(pool)
      .setPoolFees(c.protocolSpreadFeeE6 + c.adminSpreadFeeE6, c.protocolNotionalFeeE8 + c.adminNotionalFeeE8);
    emit PoolAdminSpreadFeeUpdated(pool, newAdminSpreadFeeE6);
    emit PoolAdminNotionalFeeUpdated(pool, newAdminNotionalFeeE8);
  }
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L450-457)
```text
  function setPoolBinAdditionalFees(address pool, int8 bin, uint16 addFeeBuyE6, uint16 addFeeSellE6)
    external
    override
    nonReentrant
    onlyPoolAdmin(pool)
  {
    IMetricOmmPoolFactoryActions(pool).setBinAdditionalFees(bin, addFeeBuyE6, addFeeSellE6);
  }
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L487-490)
```text
    uint256 executeAfter = block.timestamp + timelock;
    pendingPriceProvider[pool] = newPriceProvider;
    pendingPriceProviderExecuteAfter[pool] = executeAfter;
    emit PoolPriceProviderChangeProposed(pool, current, newPriceProvider, executeAfter);
```

**File:** metric-core/contracts/MetricOmmPool.sol (L540-548)
```text
    uint256 buyFeeX64 = baseFeeX64 + Math.mulDiv(binState.addFeeBuyE6, ONE_X64, 1e6);
    uint256 sellFeeX64 = baseFeeX64 + Math.mulDiv(binState.addFeeSellE6, ONE_X64, 1e6);

    uint256 askBeforeNotional = Math.mulDiv(marginalPriceX64, ONE_X64 + buyFeeX64, ONE_X64, Math.Rounding.Ceil);
    uint256 bidAfterSpread = Math.mulDiv(marginalPriceX64, ONE_X64, ONE_X64 + sellFeeX64, Math.Rounding.Floor);

    uint256 nf = notionalFeeE8;
    buyPriceX64 = Math.mulDiv(askBeforeNotional, 1e8, 1e8 - nf, Math.Rounding.Ceil).toUint128();
    sellPriceX64 = Math.mulDiv(bidAfterSpread, 1e8 - nf, 1e8, Math.Rounding.Floor).toUint128();
```

**File:** metric-core/contracts/MetricOmmPool.sol (L756-761)
```text
            uint256 notionalFeeScaled = uint256(-amount1DeltaScaled) * notionalFeeE8 / 1e8;
            if (notionalFeeScaled > 0) {
              // safe because notionalFeeScaled is bounded by uint128
              // forge-lint: disable-next-line(unsafe-typecast)
              amount1DeltaScaled = amount1DeltaScaled + int256(notionalFeeScaled);
              notionalFeeToken1Scaled = (uint256(notionalFeeToken1Scaled) + notionalFeeScaled).toUint128();
```

**File:** metric-core/contracts/MetricOmmPool.sol (L906-915)
```text
          (curPosInBinCache, delta0Scaled, delta1Scaled, binLpFeeAmountScaled) = SwapMath.buyToken0InBinSpecifiedOut(
            binState,
            curPosInBinCache,
            state,
            params.baseFeeX64 + Math.mulDiv(binState.addFeeBuyE6, ONE_X64, 1e6),
            lowerPriceX64,
            upperPriceX64,
            params.priceLimitX64,
            spreadFeeE6
          );
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L192-198)
```text
        if (block.timestamp < lastConfidenceUpdate + CONFIDENCE_COOLDOWN) {
            revert CooldownNotElapsed();
        }

        confidenceParam = newValue;
        lastConfidenceUpdate = block.timestamp;
        emit ConfidenceParamSet(newValue);
```
