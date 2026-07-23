### Title
Missing Chainlink Sequencer Uptime Feed Check Allows Stale Pre-Downtime Price Acceptance During L2 Restart Window — (`smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

---

### Summary

`ProtectedPriceProviderL2` is the L2-specific price provider for the Metric OMM protocol. It includes `FUTURE_TOLERANCE` to handle L2 sequencer timestamp lag, but contains **no Chainlink sequencer uptime feed check**. The only staleness guard is a time-delta comparison between the oracle's `refTime` and `block.timestamp`. During a sequencer downtime + restart window, a pre-downtime price whose `refTime` is still within `MAX_TIME_DELTA` passes all checks and is used for swap pricing, enabling bad-price execution against LPs.

---

### Finding Description

`ProtectedPriceProviderL2._computeBidAsk` applies a single staleness check:

```solidity
if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
    return (0, type(uint128).max);
}
``` [1](#0-0) 

`_isStale` only checks whether `nowTs - refTime > MAX_TIME_DELTA`:

```solidity
return (nowTs - refTime) > maxDelta;
``` [2](#0-1) 

`MAX_TIME_DELTA` is set at construction and can be up to 7 days:

```solidity
if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
``` [3](#0-2) 

The `IOffchainOracle` is a **push oracle** — prices are stored on-chain and read via `price(feedId, pool)`:

```solidity
function price(bytes32 feedId, address pool)
    external
    returns (uint256 mid, uint256 spread, uint16 spread1, uint256 refTime);
``` [4](#0-3) 

During sequencer downtime, no new prices can be pushed to `IOffchainOracle`. The stored `refTime` is frozen at the last pre-downtime update. When the sequencer restarts, `nowTs - refTime` equals the downtime duration. If that duration is less than `MAX_TIME_DELTA`, the stale price passes the check and is used to compute bid/ask for the swap.

A grep across the entire codebase for `sequencer`, `uptimeFeed`, `AggregatorV3`, and `sequencerFeed` in `.sol` files confirms **no sequencer uptime feed check exists anywhere** — only two comment-level mentions in `ProtectedPriceProviderL2.sol` acknowledging L2 sequencer timestamp lag for `FUTURE_TOLERANCE` purposes. [5](#0-4) 

---

### Impact Explanation

- **Bad-price execution**: A stale pre-downtime oracle price (e.g., ETH at $3,000 before downtime, market moved to $2,700 during downtime) passes all staleness, guard, and validity checks and is used to compute bid/ask for a pool swap.
- **LP principal extraction**: An attacker swaps at the stale favorable price immediately after sequencer restart, before the oracle updater can push a fresh price. LPs receive less than fair value for the assets they provide.
- **Severity**: High — direct loss of LP principal via bad-price execution, which is explicitly in the allowed impact list.

---

### Likelihood Explanation

L2 sequencer outages are documented and recurring (Arbitrum, Optimism, Base have all experienced them). The attack window is the gap between sequencer restart and the first successful oracle push. With `MAX_TIME_DELTA` up to 7 days, even a multi-hour outage leaves the stale price fully valid. The attacker only needs to monitor the sequencer status and submit a swap transaction at restart.

---

### Recommendation

Add a Chainlink sequencer uptime feed check in `_computeBidAsk` (or in `getBidAndAskPrice`) before accepting any oracle price on L2:

```solidity
// Example pattern (Chainlink L2 sequencer uptime feed)
(, int256 answer, uint256 startedAt, ,) = sequencerUptimeFeed.latestRoundData();
if (answer != 0) revert SequencerDown();
if (block.timestamp - startedAt < GRACE_PERIOD) revert GracePeriodNotOver();
```

Store `sequencerUptimeFeed` as an immutable address set at construction. Reject any price read while the sequencer is down or within the grace period after restart (Chainlink recommends ≥ 1 hour grace period).

---

### Proof of Concept

```solidity
// Foundry integration test sketch
function test_stalePrice_acceptedAfterSequencerRestart() public {
    // 1. Oracle pushed at t=0 with price=3000e8
    oracle.setData(FEED_ID, 3000e8, 300, 0, block.timestamp);
    provider.setConfidenceParam(100_000);

    // 2. Simulate sequencer downtime: advance time by (MAX_TIME_DELTA - 1)
    //    without pushing a new oracle price
    vm.warp(block.timestamp + MAX_TIME_DELTA - 1);

    // 3. Sequencer "restarts" — attacker immediately calls getBidAndAskPrice
    //    refTime = 0, nowTs = MAX_TIME_DELTA - 1
    //    nowTs - refTime = MAX_TIME_DELTA - 1 < MAX_TIME_DELTA → NOT stale
    (uint128 bid, uint128 ask) = poolContract.getBidAndAskPrice();

    // 4. Assert: stale pre-downtime price accepted, bid/ask computed from 3000e8
    assertGt(bid, 0, "stale price accepted as valid");
    // Market price is now 2700e8 — attacker swaps at 3000e8, extracting LP value
}
```

The test demonstrates that `getBidAndAskPrice` succeeds with a price that is `MAX_TIME_DELTA - 1` seconds old, which is the exact pre-downtime price. No sequencer uptime check blocks the read. [6](#0-5) [7](#0-6)

### Citations

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L40-42)
```text
    /// @dev L2 sequencer timestamp can lag behind oracle publication time.
    ///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
    uint256 public immutable FUTURE_TOLERANCE;
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L96-99)
```text
        if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
        if (_futureTolerance > 1 hours) revert FutureToleranceOutOfBounds();
        MAX_TIME_DELTA   = _maxTimeDelta;
        FUTURE_TOLERANCE = _futureTolerance;
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L130-133)
```text
    function getBidAndAskPrice() external override returns (uint128 bid, uint128 ask) {
        (bid, ask) = _getBidAndAskPrice();
        if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
    }
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L151-153)
```text
        // refTime in the past or equal: check age
        return (nowTs - refTime) > maxDelta;
    }
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L196-200)
```text
    function _getBidAndAskPrice() internal returns (uint128, uint128) {
        (uint256 mid, uint256 spread, , uint256 refTime) =
            IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);
        return _computeBidAsk(mid, spread, refTime);
    }
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L206-209)
```text
        // 1. Staleness check
        if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
            return (0, type(uint128).max);
        }
```

**File:** smart-contracts-poc/contracts/interfaces/IPricedOracle.sol (L11-13)
```text
    function price(bytes32 feedId, address pool)
        external
        returns (uint256 mid, uint256 spread, uint16 spread1, uint256 refTime);
```
