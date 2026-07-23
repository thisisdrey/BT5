### Title
Missing L2 Sequencer Uptime Check in `PriceProviderL2` and `ProtectedPriceProviderL2` Allows Stale Bid/Ask Prices to Reach Pool Swaps - (`smart-contracts-poc/contracts/PriceProviderL2.sol`, `smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

---

### Summary

`PriceProviderL2` and `ProtectedPriceProviderL2` are the L2-specific price providers deployed on Arbitrum, Base, Avalanche, BSC, and Berachain. Both implement an L2-aware staleness check (`_isStale` with `FUTURE_TOLERANCE`) to tolerate clock skew, but neither checks whether the L2 sequencer is currently up or has been up for a sufficient grace period. When the sequencer goes offline and then recovers, the last pre-downtime oracle price — which may be significantly stale relative to actual market conditions — passes the `MAX_TIME_DELTA` staleness check and is delivered as a valid bid/ask to the pool swap, enabling bad-price execution.

---

### Finding Description

Both L2 providers read oracle data and apply a time-delta staleness check:

```solidity
// PriceProviderL2.sol and ProtectedPriceProviderL2.sol — _getBidAndAskPrice()
(uint256 mid, uint256 spread, , uint256 refTime) =
    IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);

if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
    return (0, type(uint128).max);
}
```

The `_isStale` function only compares `refTime` against `block.timestamp`:

```solidity
function _isStale(uint256 refTime, uint256 nowTs, uint256 maxDelta, uint256 futureTol)
    internal pure returns (bool)
{
    if (refTime == 0) return true;
    if (refTime > nowTs) return (refTime - nowTs) > futureTol;
    return (nowTs - refTime) > maxDelta;
}
```

There is no call to a Chainlink L2 Sequencer Uptime Feed and no grace-period enforcement after sequencer recovery. The `grep` search across all production `.sol` files in `smart-contracts-poc/contracts/` confirms that `sequencerUptimeFeed` and `GRACE_PERIOD` appear only in comments about clock skew — not in any live sequencer-status check.

The oracle is push-based (Pyth / Chainlink Data Streams). While the sequencer is down, no price updates can be pushed. When the sequencer recovers, `block.timestamp` resumes, but the stored `refTime` is the timestamp of the last pre-downtime push. If the downtime was shorter than `MAX_TIME_DELTA` (which the constructor allows up to 7 days), the stale price passes the check and is returned as a valid bid/ask. Even with a short `MAX_TIME_DELTA`, the first block after recovery can execute swaps against the stale price before any updater can push a fresh one.

The Chainlink-recommended pattern — read the sequencer uptime feed, assert `answer == 0` (sequencer up), and enforce a `GRACE_PERIOD` (typically 3600 s) after `startedAt` — is entirely absent from both providers.

---

### Impact Explanation

A pool swap calls `getBidAndAskPrice()` on the L2 provider, which calls `_getBidAndAskPrice()`, which calls `_computeBidAsk(mid, spread, refTime)`. If the sequencer was down and the stale `refTime` is within `MAX_TIME_DELTA`, the stale mid price passes all guards (staleness, price guard, spread check) and is converted to a Q64 bid/ask that the pool's swap math treats as the current executable price. A trader who monitors the sequencer recovery can immediately submit a swap at the pre-downtime price before any fresh oracle push, extracting value from LPs at a price that does not reflect current market conditions. This is a direct loss of LP principal — the pool settles a trade at a stale price, and the LP bears the difference.

---

### Likelihood Explanation

L2 sequencer outages are documented, recurring events on Arbitrum, Optimism, and Base. The attack requires only a public swap call timed to the first block after sequencer recovery — no privileged access, no special tokens, no malicious setup. Any market participant monitoring the sequencer status feed can execute this. The window is bounded by how quickly an oracle updater can push a fresh price after recovery, which is typically at least one block.

---

### Recommendation

Add a sequencer uptime check at the top of `_getBidAndAskPrice()` in both `PriceProviderL2` and `ProtectedPriceProviderL2`, following the Chainlink L2 Sequencer Uptime Feed pattern:

```solidity
// Store as immutable in constructor
AggregatorV3Interface public immutable sequencerUptimeFeed;
uint256 public constant GRACE_PERIOD = 3600; // 1 hour

function _checkSequencer() internal view {
    (, int256 answer, uint256 startedAt,,) = sequencerUptimeFeed.latestRoundData();
    // answer == 0: sequencer is up; answer == 1: sequencer is down
    if (answer != 0) revert SequencerDown();
    if (block.timestamp - startedAt < GRACE_PERIOD) revert GracePeriodNotOver();
}
```

Call `_checkSequencer()` before the oracle read in `_getBidAndAskPrice()`. The `sequencerUptimeFeed` address should be passed as a constructor immutable (the `PriceProviderFactoryL2` already has the infrastructure to pass it). If `sequencerUptimeFeed` is `address(0)` (L1 deployment), skip the check.

---

### Proof of Concept

1. Pool on Arbitrum uses `ProtectedPriceProviderL2` with `MAX_TIME_DELTA = 2 hours`.
2. At `T=0`, oracle price is pushed: `mid = 3000 USDC/ETH`, `refTime = T`.
3. At `T=30min`, the Arbitrum sequencer goes offline. No new price pushes are possible.
4. Off-chain, ETH price drops to `2700 USDC/ETH`.
5. At `T=90min`, the sequencer recovers. `block.timestamp ≈ T+90min`.
6. Attacker immediately calls `pool.swap(...)` (exactInput, sell ETH for USDC).
7. Pool calls `provider.getBidAndAskPrice()` → `_getBidAndAskPrice()` → oracle returns `mid=3000, refTime=T`.
8. `_isStale(T, T+90min, 2h, futureTol)` → `(90min) <= 2h` → **not stale, passes**.
9. Pool executes the swap at the stale `3000` price. Attacker receives ~300 USDC more per ETH than the current market price. LPs absorb the loss. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L36-38)
```text
    /// @dev L2 sequencer timestamp can lag behind oracle publication time.
    ///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
    uint256 public immutable FUTURE_TOLERANCE;
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
