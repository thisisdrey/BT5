After thorough analysis of the price-provider and factory code across all three modules, here is my assessment:

**Traced paths investigated:**

1. `PriceProvider._getBidAndAskPrice()` — staleness, price guard, `bidOut >= askOut` sentinel, `confidenceParam` bounds
2. `AnchoredPriceProvider._computeBidAsk()` — reference band, source clamp, `_shapedQuote`, `_readSource`
3. `PriceProviderFactory.createPriceProvider` / `setConfidence` — permissionless creation, updater gating
4. `AnchoredProviderFactory.createAnchoredProvider` / `setSource` — oracle allow-list, envelope validation, instant source swap
5. `MetricOmmPoolFactory._validatePriceProvider` / `proposePoolPriceProvider` / `executePoolPriceProviderUpdate` — token match, timelock

**Key invariants checked:**

- `PriceProvider`: `confidenceParam = 0` (default) with `marginStep = 0` produces `bidOut == askOut` for exact mid values → stall sentinel fires, pool reverts `FeedStalled`. For non-exact mids, floor/ceil differ by 1 unit (valid, near-zero spread). No fund loss path. [1](#0-0) 

- `AnchoredPriceProvider` source clamp: `bidOut = Math.min(refBid, cBid)` and `askOut = Math.max(refAsk, cAsk)` guarantee the source can only widen the spread beyond the reference band, never tighten it. A malicious source returning `(1, type(uint128).max-1)` is bounded by `_readSource`'s `srcAsk > type(uint128).max` check and the clamp restores `bidOut <= refBid < refAsk <= askOut`. Traders get worse prices; LP principal is not drained. [2](#0-1) 

- `_readSource` rejects zero bid, inverted quotes, and uint128 overflow before the clamp is applied. [3](#0-2) 

- `_shapedQuote` deliberately omits the `sBid >= sAsk` pre-clamp halt; the band clamp in `_

### Citations

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L226-231)
```text
        // 7. Hard invariant: bid must be strictly less than ask.
        //    Can be violated when marginStep < 0 and confidence is too small.
        if (bidOut >= askOut) return (0, type(uint128).max);

        return (uint128(bidOut), uint128(askOut));
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

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L404-410)
```text
        if (!success || retSize != 64) return (false, 0, 0);

        srcBid = b;
        srcAsk = a;
        if (srcBid == 0 || srcBid >= srcAsk || srcAsk > type(uint128).max) return (false, 0, 0);

        return (true, srcBid, srcAsk);
```
