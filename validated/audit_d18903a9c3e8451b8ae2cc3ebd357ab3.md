After exhaustive tracing of every parameter-binding path in the Metric OMM oracle and factory code — `AnchoredPriceProvider`, `AnchoredProviderFactory`, `PriceProvider`, `PriceProviderFactory`, `PriceProviderFactoryL2`, `ProtectedPriceProvider`, and the pool's `_validatePriceProvider` — no valid analog to the external bug exists in this repository.

**Why each candidate path is clean:**

- **Factory → constructor bindings**: `AnchoredProviderFactory.createAnchoredProvider` passes `(address(this), oracle, baseFeedId, quoteFeedId, minMargin, maxRefStaleness, maxSpreadBps, mutableParams, marginStep, baseToken, quoteToken)` in exact positional order matching the `AnchoredPriceProvider` constructor. [1](#0-0)  Same alignment holds for `PriceProviderFactory` → `PriceProvider` and `PriceProviderFactoryL2` → `PriceProviderL2`. [2](#0-1) 

- **Pool token-pair validation**: `_validatePriceProvider` enforces `provider.token0() == pool.token0` and `provider.token1() == pool.token1` at both pool creation and every mutable-provider update, so a misbound token pair cannot reach a live swap. [3](#0-2) 

- **Oracle return-value binding**: Every provider reads `(mid, spreadBps, , refTime)` from `IPricedOracle.price(feedId, pool)` with the third slot consistently ignored; staleness is checked on `refTime` inside `_readLeg`/`_getBidAndAskPrice` before any bid/ask computation. [4](#0-3) 

- **Source-mode assembly**: `_readSource` uses `IAnchorSource.getBidAndAskPrice.selector`, caps output at 64 bytes, and rejects any result where `srcBid == 0 || srcBid >= srcAsk || srcAsk > type(uint128).max` — all failure modes close the swap. [5](#0-4) 

- **Band clamp direction**: `bidOut = Math.min(refBid, cBid)` and `askOut = Math.max(refAsk, cAsk)` correctly enforce the "never tighter than the band" invariant regardless of source output; the subsequent `bidOut >= askOut` guard closes any residual inversion. [6](#0-5) 

- **`_shapedQuote` overflow guard**: The pre-clamp `sAsk > type(uint128).max` check is present and correctly positioned before the clamp, preventing a truncated ask from escaping the band. [7](#0-6) 

No function in the production swap path receives a parameter that was removed, reordered, or misbound relative to its callee's current signature. Every guard that would be the analog of the vyper `evm_version

### Citations

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L182-194)
```text
        AnchoredPriceProvider p = new AnchoredPriceProvider(
            address(this),
            oracle,
            baseFeedId,
            quoteFeedId,
            minMargin,
            maxRefStaleness,
            maxSpreadBps,
            mutableParams,
            marginStep,
            baseToken,
            quoteToken
        );
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L49-57)
```text
        PriceProvider p = new PriceProvider(
            address(this),
            _oracle,
            _feedId,
            _marginStep,
            _maxTimeDelta,
            _baseToken,
            _quoteToken
        );
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L541-545)
```text
  function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
      revert PriceProviderTokenMismatch();
    }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L277-295)
```text
    function _readLeg(bytes32 feedId)
        internal returns (uint256 mid, uint256 spreadBps, uint256 refTime, bool ok)
    {
        (mid, spreadBps, , refTime) = IPricedOracle(address(offchainOracle)).price(feedId, msg.sender);

        // Stale reference → not ok. Clamping to a stale anchor is the one false-safety case.
        if (_isStale(refTime, block.timestamp, MAX_REF_STALENESS)) return (mid, spreadBps, refTime, false);

        // Basic validity — mid positive, spreadBps not the stalled/off-hours marker (the Chainlink oracle
        // writes spreadBps = ORACLE_BPS when an RWA market is closed).
        if (mid == 0 || spreadBps >= ORACLE_BPS) return (mid, spreadBps, refTime, false);

        // Per-leg price guard.
        (uint128 guardMin, uint128 guardMax) = offchainOracle.priceGuard(feedId);
        guardMax = guardMax == 0 ? type(uint128).max : guardMax;
        if (mid < guardMin || mid > guardMax) return (mid, spreadBps, refTime, false);

        ok = true;
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

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L385-411)
```text
    function _readSource(address _source)
        internal view returns (bool ok, uint256 srcBid, uint256 srcAsk)
    {
        bytes4 sel = IAnchorSource.getBidAndAskPrice.selector;
        bool success;
        uint256 retSize;
        uint256 b;
        uint256 a;
        assembly ("memory-safe") {
            // Scratch beyond the free-memory pointer; never updated, so this is memory-safe.
            let ptr := mload(0x40)
            mstore(ptr, sel) // 4-byte selector, left-aligned
            // Input is consumed before output is written, so in/out may share ptr. Output is capped
            // at 0x40 bytes: a larger returndata is NOT copied (only returndatasize() reports it).
            success := staticcall(SOURCE_GAS_LIMIT, _source, ptr, 0x04, ptr, 0x40)
            retSize := returndatasize()
            b := mload(ptr)
            a := mload(add(ptr, 0x20))
        }
        if (!success || retSize != 64) return (false, 0, 0);

        srcBid = b;
        srcAsk = a;
        if (srcBid == 0 || srcBid >= srcAsk || srcAsk > type(uint128).max) return (false, 0, 0);

        return (true, srcBid, srcAsk);
    }
```
