After exhaustively tracing every relevant path in the codebase, here is my analysis:

**Step 1 — Classify the external bug into one Metric OMM invariant.**

The external bug is a *misbound threshold*: `_computeCollateralSurplus` uses a hardcoded 100% CR as the surplus floor, but `processSurplus` checks against the configurable `surplusBufferRatio`. When `surplusBufferRatio > 100%`, the surplus computed is always too large, the post-mint CR falls below `surplusBufferRatio`, and the transaction always reverts. The invariant class is: **calculation threshold ≠ check threshold → bad-price/bad-amount execution or permanent DoS**.

**Step 2 — Map to Metric OMM scoped files/functions.**

The candidate paths are:
- `AnchoredPriceProvider._computeBidAsk` / `_shapedQuote` / `_readSource` — band clamp, source clamp, shaped-quote path
- `PriceProvider._getBidAndAskPrice` — `confidenceParam` × `marginStep` interaction
- `ProtectedPriceProvider._computeBidAsk` — same
- `MetricOmmPoolFactory.setPoolProtocolFee` / `setPoolAdminFees` — fee cap vs. stored fee mismatch
- `MetricOmmPool.collectFees` — surplus computation vs. fee split

**Step 3 — Trace each path for a threshold mismatch.**

`AnchoredPriceProvider._computeBidAsk`: [1](#0-0) 
The circuit breaker uses `MAX_SPREAD_BPS` (immutable). The band uses `half = spreadBps * ONE_BPS_E18 + minMargin`. The constructor guarantees `MAX_SPREAD_BPS * ONE_BPS_E18 + minMargin < BPS_BASE_U`, so `half < BPS_BASE_U` whenever `spreadBps ≤ MAX_SPREAD_BPS`. No mismatch. [2](#0-1) 

The clamp: [3](#0-2) 
`bidOut = min(refBid, cBid) ≤ refBid < refAsk ≤ max(refAsk, cAsk) = askOut`. Ordering is preserved by construction. No mismatch.

`_shapedQuote` deliberately omits a `sBid ≥ sAsk` pre-clamp halt; the band clamp in `_computeBidAsk` restores ordering. This is documented and correct. [4](#0-3) 

`PriceProvider._getBidAndAskPrice` — with `confidenceParam = 0` and `marginStep ≤ 0`, `bidOut ≥ askOut` and the provider stalls. This is documented behavior ("Can be violated when marginStep < 0 and confidence is too small") and is not a threshold mismatch between a calculation and a subsequent check. [5](#0-4) 

`MetricOmmPool.collectFees` — surplus is `balance - binTotals - notionalFees`; the fee split uses `adminSpreadFeeE6_ / spreadSumE6` and `protocolSpreadFeeE6_ / spreadSumE6`. Both numerator and denominator come from the same `poolFeeConfig` snapshot passed by the factory. No mismatch.
<cite repo="Lauraivanka/2026-07-metric-dev-oyakhil-main--014" path="metric-core/contracts/MetricOmmPool.sol" start="385" end="

### Citations

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L158-161)
```text
        // (never a tighter-than-band quote) — the clamp + that halt are the safety net, not a positive floor.
        // Worst-case half-width must stay below 100% so the clamped bid is always positive.
        if (uint256(_maxSpreadBps) * ONE_BPS_E18 + _minMargin >= BPS_BASE_U) revert BandTooWide();
        minMargin = _minMargin;
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L302-313)
```text
        // Circuit breaker: extreme (combined) uncertainty means the feed is clearly broken.
        if (spreadBps > MAX_SPREAD_BPS) {
            return (0, type(uint128).max);
        }

        // Reference band: mid ± (spreadBps + minMargin), bid rounded down, ask rounded up.
        uint256 half = spreadBps * ONE_BPS_E18 + minMargin; // < BPS_BASE_U by construction (spreadBps <= MAX_SPREAD_BPS here)
        uint256 refBid = _bandEdge(mid, BPS_BASE_U - half, Math.Rounding.Floor);
        uint256 refAsk = _bandEdge(mid, BPS_BASE_U + half, Math.Rounding.Ceil);
        if (refBid == 0 || refAsk > type(uint128).max || refBid >= refAsk) {
            return (0, type(uint128).max);
        }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L341-348)
```text
        //    bid ≤ refBid < refAsk ≤ ask, so bid < ask holds by construction.
        uint256 bidOut = Math.min(refBid, cBid);
        uint256 askOut = Math.max(refAsk, cAsk);
        if (bidOut == 0 || bidOut >= askOut) {
            return (0, type(uint128).max);
        }

        return (uint128(bidOut), uint128(askOut));
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L360-372)
```text
    function _shapedQuote(uint256 mid, uint256 spreadBps)
        internal view returns (bool ok, uint256 sBid, uint256 sAsk)
    {
        uint256 delta = mid * (spreadBps * confidenceParam) / CONFIDENCE_BASE;
        uint256 bid8 = delta >= mid ? 0 : mid - delta;
        uint256 ask8 = mid + delta;

        sBid = _bandEdge(bid8, stepBidFactor, Math.Rounding.Floor);
        sAsk = _bandEdge(ask8, stepAskFactor, Math.Rounding.Ceil);
        if (sBid == 0 || sAsk > type(uint128).max) return (false, 0, 0);

        return (true, sBid, sAsk);
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L215-228)
```text
        //    confidenceParam multiplies oracle spread; 0 means no spread
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);

        // 6. Apply marginStep adjustment
        (uint256 bidOut, bool bidOk) = _applyBidAdjustments(bid);
        if (!bidOk || bidOut > type(uint128).max) return (0, type(uint128).max);

        (uint256 askOut, bool askOk) = _applyAskAdjustments(ask);
        if (!askOk || askOut > type(uint128).max) return (0, type(uint128).max);

        // 7. Hard invariant: bid must be strictly less than ask.
        //    Can be violated when marginStep < 0 and confidence is too small.
        if (bidOut >= askOut) return (0, type(uint128).max);
```
