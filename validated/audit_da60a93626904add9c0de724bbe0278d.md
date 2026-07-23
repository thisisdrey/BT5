### Title
Missing Sequencer Uptime Check in L2 Price Providers Allows Stale Bid/Ask to Reach Pool Swaps - (File: `smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`, `smart-contracts-poc/contracts/PriceProviderL2.sol`)

---

### Summary

`ProtectedPriceProviderL2` and `PriceProviderL2` are deployed on L2 networks (Arbitrum, Base, Avalanche, BSC, Berachain) but contain no Chainlink sequencer uptime feed check. After an L2 sequencer outage, the last on-chain oracle price — which may be arbitrarily stale relative to actual market conditions — passes the `_isStale()` check if the downtime is shorter than `MAX_TIME_DELTA` (configurable up to 7 days). A `ChainlinkVerifierL2` contract exists in the registry but is not integrated into either L2 provider. Swaps execute against the pre-downtime bid/ask with no grace period enforcement.

---

### Finding Description

Both L2 price providers implement an `_isStale()` function that checks only whether the oracle's `refTime` is within `MAX_TIME_DELTA` of `block.timestamp`:

```solidity
// ProtectedPriceProviderL2.sol _computeBidAsk, line 207
if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
    return (0, type(uint128).max);
}
```

`MAX_TIME_DELTA` is bounded only at construction time to `(0, 7 days]`:

```solidity
if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
```

Neither contract stores nor queries a `sequencerUptimeFeed`. The only sequencer-related logic is `FUTURE_TOLERANCE` (max 1 hour), which is designed to tolerate oracle clock skew — not to detect sequencer downtime:

```solidity
/// @dev L2 sequencer timestamp can lag behind oracle publication time.
///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
uint256 public immutable FUTURE_TOLERANCE;
```

A grep across all `.sol` files in `smart-contracts-poc/contracts/` for `sequencerUptimeFeed`, `GRACE_PERIOD`, and `ChainlinkVerifier` returns zero source-file matches — only comments about "sequencer clock skew." The `ChainlinkVerifierL2` contract visible in the registry ABI is a standalone contract with no integration path into either L2 provider.

**Attack path:**

1. L2 sequencer goes down. No new oracle prices can be submitted on-chain. The last on-chain price has `refTime = T_last`.
2. Sequencer comes back online at `T_resume`. If `T_resume - T_last < MAX_TIME_DELTA`, the staleness check passes.
3. An attacker immediately calls `pool.swap()`. The pool calls `getBidAndAskPrice()` on the L2 provider, which calls `_computeBidAsk(mid, spread, refTime)` with the pre-downtime price.
4. The staleness check passes. The pre-downtime bid/ask is returned and consumed by the pool's swap math.
5. The attacker trades at the stale price, extracting value from LPs.

---

### Impact Explanation

The pool executes swaps against a bid/ask derived from a price that may be significantly diverged from the current market price. LPs bear the loss: the pool pays out more output tokens than the current market price warrants (or accepts fewer input tokens), directly reducing LP principal. This is a direct loss of user/LP assets matching the "bad-price execution: stale bid/ask quote reaches a pool swap" impact gate.

---

### Likelihood Explanation

L2 sequencer outages are documented historical events on Arbitrum, Base, and Optimism. The window of exploitability is the entire post-downtime period until the oracle price is refreshed on-chain. With `MAX_TIME_DELTA` up to 7 days, even a multi-hour outage leaves the stale price valid. The swap is a standard public call requiring no special role. Any MEV bot monitoring sequencer status can automate the attack.

---

### Recommendation

Integrate a sequencer uptime check into `_computeBidAsk` in both `ProtectedPriceProviderL2` and `PriceProviderL2`, following the pattern already present in the standalone `ChainlinkVerifierL2`:

```solidity
// At the top of _computeBidAsk / _getBidAndAskPrice:
(, int256 answer, uint256 startedAt, ,) = sequencerUptimeFeed.latestRoundData();
if (answer != 0) return (0, type(uint128).max);                    // sequencer is down
if (block.timestamp - startedAt < GRACE_PERIOD) return (0, type(uint128).max); // grace period
```

The `sequencerUptimeFeed` address and `GRACE_PERIOD` (minimum 1 hour, matching the external report's recommendation) should be immutables set at construction time. The factory (`PriceProviderFactoryL2`) already accepts a `_sequencerUptimeFeed` constructor parameter per the registry ABI — it must be forwarded into the created provider instances.

---

### Proof of Concept

```solidity
// Scenario: MAX_TIME_DELTA = 1 hour, sequencer down for 30 minutes

// T=0: Last oracle price submitted on-chain: mid=2000e8 (ETH/USD), refTime=0
// T=1800: Sequencer comes back online. block.timestamp=1800, refTime=0.
//         _isStale(0, 1800, 3600, futureTol) => (1800 - 0) = 1800 < 3600 => NOT stale
// Attacker calls pool.swap() immediately.
// Pool calls provider.getBidAndAskPrice() => returns bid/ask based on mid=2000e8
// Actual market price is 2200e8 (moved during downtime).
// Attacker buys ETH at 2000 instead of 2200, extracting ~9% from LP reserves.
// No sequencer check exists to block this execution.
``` [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5)

### Citations

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L38-42)
```text
    uint256 public immutable MAX_TIME_DELTA;

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

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L36-38)
```text
    /// @dev L2 sequencer timestamp can lag behind oracle publication time.
    ///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
    uint256 public immutable FUTURE_TOLERANCE;
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
