### Title
Single `MAX_REF_STALENESS` Applied to Both Legs of Synthetic Ratio Allows Stale Quote-Feed Price to Reach Pool Swap — (`smart-contracts-poc/contracts/AnchoredPriceProvider.sol`)

---

### Summary

`AnchoredPriceProvider` uses one immutable `MAX_REF_STALENESS` for both the `baseFeedId` and `quoteFeedId` legs in synthetic ratio mode. `AnchoredProviderFactory` validates `maxRefStaleness` only against the **base** feed's envelope class; the quote feed has no separate staleness envelope. When the two feeds have different update frequencies, a quote-feed price that is stale in real-world terms but still within the shared `MAX_REF_STALENESS` window passes the per-leg staleness check, corrupts the synthetic ratio mid, and the resulting bad bid/ask reaches the pool swap.

---

### Finding Description

In `AnchoredPriceProvider._readLeg()`, the staleness check is:

```solidity
if (_isStale(refTime, block.timestamp, MAX_REF_STALENESS)) return (mid, spreadBps, refTime, false);
```

`MAX_REF_STALENESS` is a single immutable set at construction. Both `_readLeg(baseFeedId)` and `_readLeg(quoteFeedId)` use it identically. [1](#0-0) 

In `_getBidAndAskPrice()`, both legs are read through `_readLeg` and the synthetic ratio is computed:

```solidity
mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);
spreadBps += spreadBps2;
``` [2](#0-1) 

In `AnchoredProviderFactory.createAnchoredProvider()`, the envelope validation is keyed **only on `baseFeedId`**:

```solidity
bytes32 classId = feedClass[baseFeedId];
if (classId == bytes32(0)) classId = DEFAULT_CLASS;
Envelope storage env = envelopes[classId];
if (
    ...
    || maxRefStaleness < env.stalenessMin || maxRefStaleness > env.stalenessMax
    ...
) revert ParamsOutOfEnvelope();
``` [3](#0-2) 

The factory's own NatSpec confirms this design gap:

> "The envelope is keyed on `baseFeedId` (the provider's class); the ref feed only contributes its uncertainty and is validated for existence at provider construction." [4](#0-3) 

The `quoteFeedId` receives **no** separate staleness envelope validation. A permissionless creator calling `createAnchoredProvider` sets `maxRefStaleness` within the base feed's envelope (e.g., `stalenessMax = 1 hour` for a slow-updating base feed). That same 1-hour threshold is then applied to the quote feed, which may update every few seconds. When the quote feed is 45 minutes stale (within the 1-hour threshold), `_readLeg(quoteFeedId)` returns `ok = true`, and the stale `mid2` is used to compute the synthetic ratio.

The corrupted ratio flows directly into `_computeBidAsk`, which computes `refBid` and `refAsk` from it:

```solidity
uint256 refBid = _bandEdge(mid, BPS_BASE_U - half, Math.Rounding.Floor);
uint256 refAsk = _bandEdge(mid, BPS_BASE_U + half, Math.Rounding.Ceil);
``` [5](#0-4) 

In reference mode (no source), these are returned directly as the pool's bid/ask. There is no downstream clamp that corrects a stale-derived mid — the band clamp only clips a custom source output against the reference band, but the reference band itself is the corrupted value. [6](#0-5) 

---

### Impact Explanation

A stale quote-feed price within `MAX_REF_STALENESS` produces an incorrect synthetic ratio mid. The resulting bid/ask is returned to the pool and used for swap settlement. A trader who observes the stale quote-feed price can execute a swap at the incorrect rate, extracting value from LPs. The loss is proportional to the price drift of the quote asset during the staleness window multiplied by the swap size.

---

### Likelihood Explanation

- Synthetic ratio mode (`quoteFeedId != 0`) is a documented, production-intended feature.
- The two feeds in a synthetic pair (e.g., BTC/USD and ETH/USD) routinely have different update frequencies and heartbeat intervals.
- `MAX_REF_STALENESS` is calibrated for the base feed's class; the quote feed's class is never consulted.
- Any public trader can execute a swap at any time; no privileged role is required to trigger the bad-price execution.
- The window during which the quote feed is stale-but-within-threshold can last minutes to hours depending on the configured `MAX_REF_STALENESS`.

---

### Recommendation

Introduce a separate `maxQuoteStaleness` immutable for the quote feed leg. Validate it in `AnchoredProviderFactory.createAnchoredProvider()` against the envelope of `feedClass[quoteFeedId]` (or a dedicated quote-feed class). Pass it to the `AnchoredPriceProvider` constructor and use it in `_readLeg` when processing `quoteFeedId`:

```solidity
// In _getBidAndAskPrice:
if (_quote != bytes32(0)) {
    (uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLegWithStaleness(_quote, MAX_QUOTE_STALENESS);
    ...
}
```

This mirrors the Morpho fix (PR #557) of making the staleness parameter per-pool/per-feed rather than a single shared constant.

---

### Proof of Concept

1. Admin configures `DEFAULT_CLASS` envelope with `stalenessMax = 3600` (1 hour).
2. Creator calls `createAnchoredProvider(oracle, BTC_USD_FEED, ETH_USD_FEED, ..., maxRefStaleness=3600, ...)`. Factory validates `3600 <= stalenessMax` for `BTC_USD_FEED`'s class — passes. No validation for `ETH_USD_FEED`.
3. `AnchoredPriceProvider` is deployed with `MAX_REF_STALENESS = 3600`.
4. At `t=0`: both feeds are fresh. BTC/USD = 100,000e8, ETH/USD = 3,000e8. Synthetic BTC/ETH = 33.33.
5. At `t=2700` (45 min): ETH/USD feed has not updated (e.g., Pyth heartbeat missed). BTC/USD updated at `t=2700`.
6. Trader calls pool `swap()`. Pool calls `provider.getBidAndAskPrice()`.
7. `_readLeg(BTC_USD_FEED)`: `refTime = 2700`, `nowTs = 2700`, `2700 - 2700 = 0 <= 3600` → ok.
8. `_readLeg(ETH_USD_FEED)`: `refTime = 0`, `nowTs = 2700`, `2700 - 0 = 2700 <= 3600` → **ok** (stale price passes).
9. ETH has moved from 3,000 to 3,300 (+10%) but the stale price 3,000 is used.
10. Synthetic ratio = 100,000 / 3,000 = 33.33 instead of correct 100,000 / 3,300 = 30.30.
11. Pool bid/ask is computed from the inflated ratio 33.33.
12. Trader buys token0 (BTC) with token1 (ETH) at the inflated price, receiving more BTC than the correct price permits. LPs suffer the loss. [7](#0-6) [3](#0-2) [8](#0-7)

### Citations

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L64-77)
```text
    // ── Immutables ──────────────────────────────────────────────────────
    IOffchainOracle public immutable offchainOracle;
    bytes32         public immutable baseFeedId;
    /// @notice Optional second feed for synthetic ratio quoting; zero = single-feed (no conversion).
    ///         Synthetic mid = price(baseFeedId) / price(quoteFeedId), e.g. BTC/USD ÷ ETH/USD = BTC/ETH.
    bytes32         public immutable quoteFeedId;
    /// @dev anchor factory (governs setSource), NOT the AMM pool factory passed at read.
    address         public immutable factory;

    /// @notice Per-side minimum margin on top of the reference spread, BPS_BASE_U scale (1 bps = 1e14).
    uint256 public immutable minMargin;
    /// @notice Reference older than this (seconds) halts quoting — never clamp to a stale anchor.
    ///         Zero means the reference must be in the current block (refTime == block.timestamp).
    uint256 public immutable MAX_REF_STALENESS;
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L258-295)
```text
    function _getBidAndAskPrice() internal returns (uint128, uint128) {
        (uint256 mid, uint256 spreadBps, , bool ok) = _readLeg(baseFeedId);
        if (!ok) return (0, type(uint128).max);

        bytes32 _quote = quoteFeedId;
        if (_quote != bytes32(0)) {
            (uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLeg(_quote);
            if (!ok2 || mid2 == 0) return (0, type(uint128).max);
            // Synthetic ratio (8-decimal): mid1 / mid2. Relative uncertainties of a ratio add.
            mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);
            spreadBps += spreadBps2;
        }

        return _computeBidAsk(mid, spreadBps);
    }

    /// @dev Reads one feed and runs its per-leg guards. ok=false (→ caller halts, fail closed) on:
    ///      stale reference, mid == 0, spreadBps == the off-hours/stall marker (spreadBps >= ORACLE_BPS), or a
    ///      priceGuard violation. Each leg is read through the attributed path independently.
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

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L307-313)
```text
        // Reference band: mid ± (spreadBps + minMargin), bid rounded down, ask rounded up.
        uint256 half = spreadBps * ONE_BPS_E18 + minMargin; // < BPS_BASE_U by construction (spreadBps <= MAX_SPREAD_BPS here)
        uint256 refBid = _bandEdge(mid, BPS_BASE_U - half, Math.Rounding.Floor);
        uint256 refAsk = _bandEdge(mid, BPS_BASE_U + half, Math.Rounding.Ceil);
        if (refBid == 0 || refAsk > type(uint128).max || refBid >= refAsk) {
            return (0, type(uint128).max);
        }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L336-338)
```text
        } else {
            return (uint128(refBid), uint128(refAsk));
        }
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L153-155)
```text
    /// @param quoteFeedId optional second feed for synthetic ratio quoting (zero = single-feed). The
    ///        envelope is keyed on `baseFeedId` (the provider's class); the ref feed only contributes its
    ///        uncertainty and is validated for existence at provider construction.
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L170-180)
```text
        // Feeds without an explicit class fall back to the admin-configured DEFAULT_CLASS envelope.
        bytes32 classId = feedClass[baseFeedId];
        if (classId == bytes32(0)) classId = DEFAULT_CLASS;

        Envelope storage env = envelopes[classId];
        if (!env.exists) revert EnvelopeNotFound(classId);
        if (
            minMargin < env.minMarginMin || minMargin > env.minMarginMax
            || maxRefStaleness < env.stalenessMin || maxRefStaleness > env.stalenessMax
            || maxSpreadBps < env.maxSpreadMin || maxSpreadBps > env.maxSpreadMax
        ) revert ParamsOutOfEnvelope();
```
