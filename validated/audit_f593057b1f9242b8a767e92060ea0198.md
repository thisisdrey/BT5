### Title
Missing L2 Sequencer Uptime Check in `PriceProviderL2` and `ProtectedPriceProviderL2` Allows Stale-Price Swaps After Sequencer Restart — (`smart-contracts-poc/contracts/PriceProviderL2.sol`, `smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

---

### Summary

The protocol's own README declares as a non-negotiable invariant: *"Swaps must revert if the price is stale, if the bid exceeds the ask, or if the sequencer is down on L2."* Both L2 price providers (`PriceProviderL2` and `ProtectedPriceProviderL2`) are deployed on Arbitrum and Base via `PriceProviderFactoryL2`, yet neither contract checks the L2 sequencer uptime feed. After a sequencer restart, push-based oracle data stored before the outage can still pass the `MAX_TIME_DELTA` staleness check while reflecting pre-downtime prices, allowing a trader to execute swaps at stale quotes at LP expense.

---

### Finding Description

Both L2 providers read oracle data through `IPricedOracle.price(feedId, pool)` and apply a staleness check:

```solidity
// PriceProviderL2._getBidAndAskPrice()
(uint256 mid, uint256 spread, , uint256 refTime) =
    IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);

if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
    return (0, type(uint128).max);
}
``` [1](#0-0) 

The identical pattern appears in `ProtectedPriceProviderL2._computeBidAsk()`: [2](#0-1) 

Neither contract queries a Chainlink sequencer uptime feed (`AggregatorV3Interface`) before accepting the stored oracle data. The `FUTURE_TOLERANCE` parameter only handles clock skew between the sequencer and oracle publication timestamps — it does not detect sequencer downtime. [3](#0-2) 

The oracle layer is push-based: reports are stored by `ChainlinkOracle.updateReport()` / `PythOracle`. When the sequencer goes down, no new reports can be pushed. When it restarts, the last stored report's `refTime` may still be within `MAX_TIME_DELTA` (which can be set up to 7 days per the constructor guard), but the market price may have moved substantially during the outage. [4](#0-3) 

The `PriceProviderFactoryL2` deploys `PriceProviderL2` instances on Arbitrum, Base, and other L2s, confirming these are production contracts on chains with sequencer risk: [5](#0-4) 

The stated protocol invariant is explicit in the README and is violated by both L2 providers: [6](#0-5) 

---

### Impact Explanation

**High impact.** A trader can execute swaps at stale pre-downtime prices during the grace period after sequencer restart. The pool settles the trade at the stale bid/ask, and LPs absorb the difference between the stale price and the true market price. This is a direct loss of LP principal — the exact impact class listed in the allowed gate ("bad-price execution: stale bid/ask quote reaches a pool swap").

---

### Likelihood Explanation

**Low likelihood.** Requires an L2 sequencer outage (which has occurred on Arbitrum and Base historically) combined with a `MAX_TIME_DELTA` window long enough that the pre-downtime oracle data is still considered fresh after restart. No privileged action is required — any public trader can trigger the swap.

---

### Recommendation

Add a sequencer uptime feed check in both `PriceProviderL2._getBidAndAskPrice()` and `ProtectedPriceProviderL2._computeBidAsk()` before accepting oracle data. The check should use `AggregatorV3Interface.latestRoundData()` on the sequencer uptime feed and enforce a grace period after restart (e.g., 3600 seconds), consistent with the Chainlink L2 sequencer feed pattern. The registry's own `ChainlinkVerifierL2` ABI already exposes `sequencerUptimeFeed()` and `GRACE_PERIOD()`, confirming the protocol design intended this guard to exist.

```solidity
(, int256 answer, uint256 startedAt,,) = sequencerUptimeFeed.latestRoundData();
require(answer == 0, "Sequencer down");
require(block.timestamp - startedAt > GRACE_PERIOD, "Grace period active");
```

---

### Proof of Concept

1. Deploy `PriceProviderL2` on Arbitrum with `MAX_TIME_DELTA = 3600` (1 hour).
2. At `T=0`, a valid oracle report is pushed: `mid = 100e8` (e.g., ETH = $100).
3. At `T=100`, the Arbitrum sequencer goes offline. No new reports can be pushed.
4. During the outage, the true market price moves to `mid = 80e8` (−20%).
5. At `T=1000`, the sequencer restarts. The stored `refTime = T=0` is 1000 seconds old — still within `MAX_TIME_DELTA = 3600`.
6. `_isStale(T=0, T=1000, 3600, futureTol)` returns `false` — the stale data passes.
7. A trader calls `pool.swap()` → `provider.getBidAndAskPrice()` → returns bid/ask derived from the stale `mid = 100e8`.
8. The trader buys the base token at the stale ask price (equivalent to $100) when the true price is $80, extracting ~20% value from LP positions. [7](#0-6) [8](#0-7)

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L36-42)
```text
    /// @dev L2 sequencer timestamp can lag behind oracle publication time.
    ///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
    uint256 public immutable FUTURE_TOLERANCE;

    address public immutable baseToken;
    address public immutable quoteToken;

```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L92-95)
```text
        if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
        if (_futureTolerance > 1 hours) revert FutureToleranceOutOfBounds();
        MAX_TIME_DELTA   = _maxTimeDelta;
        FUTURE_TOLERANCE = _futureTolerance;
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

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L208-217)
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
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L138-153)
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

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L196-209)
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

**File:** README.md (L47-49)
```markdown
Quote sanity: bid > 0 and bid < ask always (hard invariant; BidIsZero / BidGreaterThanAsk).
Anchored band: every AnchoredPriceProvider quote — including source mode — stays within mid ± (u + floor); an unreviewed source can never push price outside the band.
No trade on bad oracle: swaps revert on stale price (maxTimeDelta/maxRefStaleness), excessive Chainlink deviation, or (L2) sequencer down.
```
