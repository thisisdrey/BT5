After a thorough investigation of the Metric OMM price-provider and factory code, I traced every path the external bug class maps to:

**Analog classification:** The SHA256 bug is a *threshold-skip returning a fixed default* — when N < BLOCK_SIZE, the loop body never executes and the canonical initial hash state `h` is returned unchanged for all small inputs. The Metric OMM analog would be: a boundary condition causes price computation to be skipped, returning a fixed/default bid/ask that reaches pool swaps.

**Paths investigated:**

1. **`AnchoredPriceProvider._computeBidAsk` clamp** — `Math.min(refBid, cBid)` / `Math.max(refAsk, cAsk)` is directionally correct. A tighter source is clipped to the band edge; a wider source passes through. The `bidOut == 0 || bidOut >= askOut` guard catches any residual inversion. [1](#0-0) 

2. **`_shapedQuote` missing pre-clamp `sBid >= sAsk` check** — Intentional and safe: when `confidenceParam = 0` and `marginStep = 0`, floor == ceil for a round mid, giving `sBid == sAsk`. The band clamp (`min`/`max` vs `refBid`/`refAsk`) restores ordering because `refBid < refAsk` is guaranteed. The comment explicitly documents this. [2](#0-1) 

3. **`_readSource` missing `srcBid > type(uint128).max` check** — Not exploitable: if `srcBid > type(uint128).max`, then `srcBid >= srcAsk` (since `srcAsk <= type(uint128).max` is enforced), so the function returns `(false, 0, 0)` and halts. [3](#0-2) 

4. **Synthetic ratio `spreadBps += spreadBps2` overflow** — If the combined spread exceeds `MAX_SPREAD_BPS`, the circuit breaker fires and returns `(0, type(uint128).max)`. The constructor's `BandTooWide` check ensures `MAX_SPREAD_BPS * ONE_BPS_E18 + minMargin < BPS_BASE_U`, preventing underflow in `BPS_BASE_U - half`. [4](#0-3) 

5. **`confidenceParam = 0` degenerate quote** — With `adjustedSpread = 0`, bid = ask = mid. For a round mid, `bidOut = askOut` triggers `bidOut >= askOut` → `FeedStalled` revert. This is a DoS on swaps, not a fund loss, and is a known/documented edge case. [5](#0-4) 

6. **All sentinel paths** — Every invalid-price branch in every provider (`PriceProvider`, `PriceProviderL2`, `ProtectedPriceProvider`, `ProtectedPriceProviderL2`, `AnchoredPriceProvider`) returns `(0, type(uint128).max)`, and `getBidAndAskPrice()` reverts with `FeedStalled` before any value reaches the pool swap. [6](#0-5) 

7. **Factory token binding** — `_validatePriceProvider` enforces `provider.token0() == pool.token0` and `provider.token1() == pool.token1` at `createPool` time. No mismatch can reach a live swap. <cite repo="ThankGodontt/2026-07-metric-dev-oyakhil-main--008" path="metric-core/contracts/MetricOmmPoolFactory.sol" start="541" end="546"

### Citations

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L214-217)
```text
    function getBidAndAskPrice() external override returns (uint128 bid, uint128 ask) {
        (bid, ask) = _getBidAndAskPrice();
        if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
    }
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

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L340-348)
```text
        // 8. Clamp: out-of-band custom quotes are clipped silently to the band edge.
        //    bid ≤ refBid < refAsk ≤ ask, so bid < ask holds by construction.
        uint256 bidOut = Math.min(refBid, cBid);
        uint256 askOut = Math.max(refAsk, cAsk);
        if (bidOut == 0 || bidOut >= askOut) {
            return (0, type(uint128).max);
        }

        return (uint128(bidOut), uint128(askOut));
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L351-372)
```text
    /// @dev Shaped reference quote (customizable variant, reference mode): delta = mid·spreadBps·confidence,
    ///      then the marginStep step factors via `_bandEdge` (byte-identical to PriceProvider's step math).
    ///
    ///      Deliberately NO `sBid >= sAsk` pre-clamp halt (unlike PriceProvider): with knobs at 0
    ///      (confidence 0, marginStep 0) and
    ///      a round mid, floor == ceil gives sBid == sAsk, and the band clamp restores ordering —
    ///      that is exactly the immutable-variant identity. The `sAsk > uint128.max` check MUST stay
    ///      pre-clamp (mirrors `_readSource`'s srcAsk guard): truncating later would let an
    ///      overflowing ask wrap to a sub-band quote.
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

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L404-410)
```text
        if (!success || retSize != 64) return (false, 0, 0);

        srcBid = b;
        srcAsk = a;
        if (srcBid == 0 || srcBid >= srcAsk || srcAsk > type(uint128).max) return (false, 0, 0);

        return (true, srcBid, srcAsk);
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L216-228)
```text
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
