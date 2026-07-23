### Title
Missing L2 Sequencer Uptime Check in L2 Price Providers Allows Stale Oracle Prices to Reach Pool Swaps — (`smart-contracts-poc/contracts/PriceProviderL2.sol`, `smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

### Summary

`PriceProviderL2` and `ProtectedPriceProviderL2` are explicitly designed for L2 deployment (they carry the `FUTURE_TOLERANCE` immutable to handle L2 sequencer clock skew) but contain no Chainlink L2 sequencer uptime feed check. When the L2 sequencer goes offline and resumes, the oracle's `refTime` may be frozen at the pre-outage timestamp. If the outage duration is shorter than `MAX_TIME_DELTA`, the existing staleness guard passes the stale price, and the pool swap executes against a price that no longer reflects the market — a direct bad-price execution impact.

### Finding Description

Both L2 providers expose the same `_isStale` helper:

```solidity
// PriceProviderL2.sol lines 135-150
function _isStale(
    uint256 refTime,
    uint256 nowTs,
    uint256 maxDelta,
    uint256 futureTol
) internal pure returns (bool) {
    if (refTime == 0) return true;
    if (refTime > nowTs) {
        return (refTime - nowTs) > futureTol;
    }
    return (nowTs - refTime) > maxDelta;
}
``` [1](#0-0) 

This is the **only** temporal guard. There is no call to a Chainlink `AggregatorV3Interface` sequencer uptime feed, no `isSequencerUp` flag, and no post-resume grace period. The comment on `FUTURE_TOLERANCE` explicitly acknowledges the L2 sequencer context:

```solidity
/// @dev L2 sequencer timestamp can lag behind oracle publication time.
///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
uint256 public immutable FUTURE_TOLERANCE;
``` [2](#0-1) 

The same structure is replicated verbatim in `ProtectedPriceProviderL2`: [3](#0-2) 

`PriceProviderFactoryL2.createPriceProvider` accepts `_maxTimeDelta` from the caller with no envelope bound beyond the provider constructor's `> 7 days` ceiling:

```solidity
if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
``` [4](#0-3) 

`PriceProviderFactoryL2` passes the caller-supplied value straight through with no additional staleness envelope: [5](#0-4) 

The downstream `_getBidAndAskPrice` in both L2 providers calls the oracle, runs `_isStale`, and — if the check passes — computes and returns bid/ask prices that flow directly to the pool swap: [6](#0-5) [7](#0-6) 

### Impact Explanation

When the L2 sequencer resumes after an outage shorter than `MAX_TIME_DELTA`, the oracle's `refTime` is frozen at the pre-outage timestamp `T`. `block.timestamp` is now `T + outage_duration`. The staleness check evaluates `(outage_duration) > MAX_TIME_DELTA` — false — so the stale price passes. `getBidAndAskPrice()` returns bid/ask values derived from the pre-outage oracle mid. If the market moved materially during the outage (e.g., a 10–20% drop), the pool swap executes at the wrong price: a buyer pays less than fair value (pool receives fewer input tokens than owed) or a seller receives more than fair value (pool pays out more output tokens than it should). This is a direct loss of pool assets — matching the "bad-price execution: stale bid/ask quote reaches a pool swap" impact gate.

### Likelihood Explanation

L2 sequencer outages are documented real events (Arbitrum suffered a ~1-hour outage in December 2023). `MAX_TIME_DELTA` is caller-configurable up to 7 days with no factory-level envelope, so a provider deployed with `MAX_TIME_DELTA = 2 hours` is fully vulnerable to any outage under 2 hours. The trigger requires no privileged action after deployment — any swap caller initiating a trade immediately after sequencer resumption is sufficient.

### Recommendation

Add a Chainlink L2 sequencer uptime feed check inside `_getBidAndAskPrice` (or a shared internal helper) in both `PriceProviderL2` and `ProtectedPriceProviderL2`:

```solidity
// Example pattern (Chainlink L2 sequencer uptime feed)
(, int256 answer, uint256 startedAt, ,) = sequencerUptimeFeed.latestRoundData();
if (answer != 0) return (0, type(uint128).max);                    // sequencer down
if (block.timestamp - startedAt < GRACE_PERIOD) return (0, type(uint128).max); // grace period
```

The sequencer uptime feed address and `GRACE_PERIOD` (typically 3600 seconds) should be immutables set at construction. `PriceProviderFactoryL2` should require a non-zero sequencer feed address and validate `GRACE_PERIOD` within an acceptable range. Alternatively, `PriceProviderFactoryL2` should enforce a staleness envelope (analogous to `AnchoredProviderFactory`'s `stalenessMin`/`stalenessMax`) so that `MAX_TIME_DELTA` cannot be set to values that dwarf realistic sequencer outage durations.

### Proof of Concept

1. Deploy `PriceProviderL2` via `PriceProviderFactoryL2.createPriceProvider` with `_maxTimeDelta = 2 hours`, `_futureTolerance = 60 seconds`.
2. Pool is configured to use this provider.
3. At time `T`: oracle reports `mid = 1000e8`, `refTime = T`. Swap prices are valid.
4. At `T + 1 min`: L2 sequencer goes offline. No transactions process.
5. During outage (`T+1min` → `T+90min`): off-chain market price falls to `800e8`. Oracle cannot be updated on-chain.
6. At `T + 90 min`: sequencer resumes. Oracle still holds `mid = 1000e8`, `refTime = T`.
7. Attacker (or any user) calls `pool.swap(...)`.
8. Pool calls `provider.getBidAndAskPrice()`.
9. Provider calls `oracle.price(feedId, pool)` → returns `(1000e8, spread, ..., T)`.
10. `_isStale(T, T+90min, 2 hours, 60s)` → `(90 min > 2 hours)` → **false** → price accepted.
11. Bid/ask computed from stale `mid = 1000e8` are returned to the pool.
12. Swap executes: attacker buys base token at the stale $1000 price while market is $800 — pool receives $1000-worth of quote but delivers $800-worth of base at current market, netting a $200/unit loss from pool reserves.

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L36-38)
```text
    /// @dev L2 sequencer timestamp can lag behind oracle publication time.
    ///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
    uint256 public immutable FUTURE_TOLERANCE;
```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L92-92)
```text
        if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L135-150)
```text
    function _isStale(
        uint256 refTime,
        uint256 nowTs,
        uint256 maxDelta,
        uint256 futureTol
    ) internal pure returns (bool) {
        if (refTime == 0) return true;

        if (refTime > nowTs) {
            // refTime in the future: tolerate only within futureTol
            return (refTime - nowTs) > futureTol;
        }

        // refTime in the past or equal: check age
        return (nowTs - refTime) > maxDelta;
    }
```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L208-248)
```text
    function _getBidAndAskPrice() internal returns (uint128, uint128) {
        // 1. Read via the unified price(feedId, pool) path, forwarding the pool (msg.sender).
        //    refTime is already in seconds.
        (uint256 mid, uint256 spread, , uint256 refTime) =
            IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);

        // 2. Staleness check
        if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
            return (0, type(uint128).max);
        }

        // 3. Basic validity — price must be positive, spread must not be stalled marker
        if (mid == 0 || spread >= ORACLE_BPS) {
            return (0, type(uint128).max);
        }

        // 4. Price guard check (moved from oracle)
        (uint128 guardMin, uint128 guardMax) = offchainOracle.priceGuard(offchainFeedId);
        guardMax = guardMax == 0 ? type(uint128).max : guardMax;
        if (mid < guardMin || mid > guardMax) {
            return (0, type(uint128).max);
        }

        // 5. Compute bid/ask from mid + confidence-adjusted spread
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

        return (uint128(bidOut), uint128(askOut));
    }
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L136-153)
```text

    /// @dev Pure staleness check. L2-aware: tolerates oracle refTime slightly ahead of block.timestamp.
    function _isStale(
        uint256 refTime,
        uint256 nowTs,
        uint256 maxDelta,
        uint256 futureTol
    ) internal pure returns (bool) {
        if (refTime == 0) return true;

        if (refTime > nowTs) {
            // refTime in the future: tolerate only within futureTol
            return (refTime - nowTs) > futureTol;
        }

        // refTime in the past or equal: check age
        return (nowTs - refTime) > maxDelta;
    }
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L196-238)
```text
    function _getBidAndAskPrice() internal returns (uint128, uint128) {
        (uint256 mid, uint256 spread, , uint256 refTime) =
            IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);
        return _computeBidAsk(mid, spread, refTime);
    }

    /// @dev Downstream pricing: staleness, price guard, confidence spread, marginStep.
    function _computeBidAsk(uint256 price, uint256 spread, uint256 refTime)
        internal view returns (uint128, uint128)
    {
        // 1. Staleness check
        if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
            return (0, type(uint128).max);
        }

        // 2. Basic validity — price must be positive, spread must not be stalled marker
        if (price == 0 || spread >= ORACLE_BPS) {
            return (0, type(uint128).max);
        }

        // 3. Price guard check
        (uint128 guardMin, uint128 guardMax) = offchainOracle.priceGuard(offchainFeedId);
        guardMax = guardMax == 0 ? type(uint128).max : guardMax;
        if (price < guardMin || price > guardMax) {
            return (0, type(uint128).max);
        }

        // 4. Compute bid/ask from mid + confidence-adjusted spread
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(price, adjustedSpread);

        // 5. Apply marginStep adjustment
        (uint256 bidOut, bool bidOk) = _applyBidAdjustments(bid);
        if (!bidOk || bidOut > type(uint128).max) return (0, type(uint128).max);

        (uint256 askOut, bool askOk) = _applyAskAdjustments(ask);
        if (!askOk || askOut > type(uint128).max) return (0, type(uint128).max);

        // 6. Hard invariant: bid must be strictly less than ask.
        if (bidOut >= askOut) return (0, type(uint128).max);

        return (uint128(bidOut), uint128(askOut));
    }
```

**File:** smart-contracts-poc/contracts/PriceProviderFactoryL2.sol (L41-59)
```text
    function createPriceProvider(
        address _oracle,
        bytes32 _feedId,
        int256  _marginStep,
        uint256 _maxTimeDelta,
        uint256 _futureTolerance,
        address _baseToken,
        address _quoteToken
    ) external override returns (address provider) {
        PriceProviderL2 p = new PriceProviderL2(
            address(this),
            _oracle,
            _feedId,
            _marginStep,
            _maxTimeDelta,
            _futureTolerance,
            _baseToken,
            _quoteToken
        );
```
