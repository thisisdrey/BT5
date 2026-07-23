### Title
Missing Sequencer Uptime Check in L2 Price Providers Allows Stale-Price Execution After Sequencer Recovery — (File: `smart-contracts-poc/contracts/PriceProviderL2.sol`, `smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

---

### Summary

`PriceProviderL2` and `ProtectedPriceProviderL2` are the designated L2 price providers for Metric OMM pools. Both implement a `FUTURE_TOLERANCE` window to tolerate L2 sequencer clock skew, but neither checks whether the L2 sequencer itself was recently down. The only staleness guard is `MAX_TIME_DELTA` (configurable up to 7 days). When the sequencer resumes after downtime shorter than `MAX_TIME_DELTA`, the last pre-downtime oracle price passes the staleness check and is forwarded as a valid bid/ask to the pool swap, enabling an attacker to trade against a price that no longer reflects the market.

---

### Finding Description

Both L2 providers share the same staleness logic:

```solidity
// PriceProviderL2.sol L135-L150
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
    return (nowTs - refTime) > maxDelta;   // ← only age check
}
``` [1](#0-0) 

This check answers only "how old is the price?" It does not answer "was the sequencer down during that interval?" When the sequencer is offline, push-oracle keepers (Pyth Lazer, Chainlink Data Streams) cannot land updates on-chain. The oracle's `refTime` freezes at the last pre-downtime update. Upon sequencer recovery, if `block.timestamp − refTime < MAX_TIME_DELTA`, `_isStale` returns `false` and the stale price is accepted.

The downstream path then computes bid/ask from that stale mid and forwards it to the pool:

```solidity
// PriceProviderL2.sol L208-L248
function _getBidAndAskPrice() internal returns (uint128, uint128) {
    (uint256 mid, uint256 spread, , uint256 refTime) =
        IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);

    if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
        return (0, type(uint128).max);   // ← only guard
    }
    // ... bid/ask computed from stale mid ...
}
``` [2](#0-1) 

`ProtectedPriceProviderL2` has the identical structure: [3](#0-2) 

The constructor enforces `MAX_TIME_DELTA ∈ (0, 7 days]` and `FUTURE_TOLERANCE ≤ 1 hour`, but places no upper bound that would make the window tight enough to always expire before a realistic sequencer restart: [4](#0-3) 

The `PriceProviderFactoryL2` imposes no additional constraint on `_maxTimeDelta` beyond the provider's own constructor check, and places no restriction on which oracle address is supplied: [5](#0-4) 

The codebase does contain a `ChainlinkVerifierL2` artifact (visible in the registry JSON) that carries `sequencerUptimeFeed` and `GRACE_PERIOD`. That check lives inside the Chainlink Data Streams oracle layer and is therefore absent when the oracle bound to the provider is Pyth Lazer or any other oracle that does not embed its own sequencer guard. Because `PriceProviderFactoryL2.createPriceProvider` accepts any `_oracle` address without verifying that it contains a sequencer check, the provider-level gap is reachable regardless of which oracle is chosen. [6](#0-5) 

---

### Impact Explanation

The stale bid/ask price returned by `getBidAndAskPrice()` is consumed directly by the pool's swap math. An attacker who submits a swap in the first block(s) after sequencer recovery — before any keeper can push a fresh oracle update — trades against the pre-downtime price. If the market moved adversely during downtime (e.g., ETH/USD fell from $2 000 to $1 800), the attacker buys ETH at $1 800 on the open market and sells it to the pool at the stale bid (~$1 990), extracting ~$190 per ETH directly from LP reserves. The loss is a direct reduction of LP principal with no recovery path.

---

### Likelihood Explanation

L2 sequencer outages are documented historical events on Arbitrum and Optimism. The attack requires no privileged access: any EOA can submit a swap immediately after the sequencer resumes. The only precondition is that the downtime duration is shorter than `MAX_TIME_DELTA` (which can be set up to 7 days). Even with a conservative 1-hour `MAX_TIME_DELTA`, any outage under 60 minutes — the majority of recorded incidents — leaves the stale price valid. The attacker needs only to monitor the sequencer status feed and have a swap ready.

---

### Recommendation

1. Add a sequencer uptime check directly inside `_getBidAndAskPrice()` in both `PriceProviderL2` and `ProtectedPriceProviderL2`, mirroring the pattern already present in `ChainlinkVerifierL2`:
   - Query the Chainlink Sequencer Uptime Feed (`AggregatorV3Interface`).
   - Revert (return the stale sentinel `(0, type(uint128).max)`) if the sequencer is currently down **or** if `block.timestamp − startedAt < GRACE_PERIOD` (e.g., 3 600 seconds).
2. Enforce that `MAX_TIME_DELTA` is no larger than the oracle's expected heartbeat plus a small buffer, so that any outage longer than one heartbeat automatically expires the price.
3. Consider requiring the factory to supply a sequencer feed address at provider creation time, making the check mandatory rather than optional.

---

### Proof of Concept

```
Setup:
  oracle = PythLazerOracle (no built-in sequencer check)
  provider = PriceProviderL2(oracle, feedId, marginStep=0,
                              maxTimeDelta=3600,   // 1 hour
                              futureTolerance=30)

T=0:    oracle.refTime = 0,  mid = 2000e8  (ETH/USD $2000)
T=0:    L2 sequencer goes offline
        Market price falls to $1800 during outage
T=1800: Sequencer resumes (30 min outage < MAX_TIME_DELTA=3600)
        No keeper has pushed a new price yet

Attacker calls pool.swap() → pool calls provider.getBidAndAskPrice()
  _isStale(refTime=0, nowTs=1800, maxDelta=3600, futureTol=30)
  → (1800 - 0) = 1800 < 3600  → NOT stale  ← stale price accepted

  mid = 2000e8, bid ≈ 1990e8 (pool buys ETH at $1990)

Attacker:
  buys 100 ETH at $1800 on market  → cost $180 000
  sells 100 ETH to pool at $1990   → receives $199 000
  profit = $19 000 extracted from LP reserves
``` [1](#0-0) [7](#0-6) [8](#0-7) [5](#0-4)

### Citations

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

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L196-210)
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

**File:** smart-contracts-poc/contracts/PriceProviderFactoryL2.sol (L41-60)
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
