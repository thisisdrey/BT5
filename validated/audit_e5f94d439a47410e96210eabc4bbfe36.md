The full `ProtectedPriceProviderL2.sol` has been read. The grep for `sequencer|uptimeFeed|gracePeriod|GRACE_PERIOD|startedAt` in the contracts directory returned only comment-level matches (the `FUTURE_TOLERANCE` clock-skew comment), confirming there is no sequencer uptime feed integration anywhere in the contract code.

---

### Title
Missing L2 Sequencer Uptime Feed Check Allows Stale Pre-Downtime Oracle Prices to Execute Swaps — (`smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

---

### Summary

`ProtectedPriceProviderL2` is the designated L2 price provider for Metric OMM pools. It adds `FUTURE_TOLERANCE` over the L1 variant to handle sequencer clock skew, but it does **not** check a Chainlink sequencer uptime feed and does **not** enforce a grace period after sequencer restart. As a result, when the L2 sequencer restarts after downtime shorter than `MAX_TIME_DELTA`, the oracle's last pre-downtime price passes the staleness check and is used to price swaps — allowing an unprivileged trader to arbitrage the pool at stale prices at the expense of LPs.

---

### Finding Description

The entire staleness defense in `ProtectedPriceProviderL2` is the `_isStale` function:

```solidity
// smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol  L138-L153
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

This is a pure time-delta check. It has no knowledge of whether the L2 sequencer was recently restarted. `_computeBidAsk` calls it as the sole sequencer-related guard:

```solidity
// L207
if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
    return (0, type(uint128).max);
}
``` [2](#0-1) 

`MAX_TIME_DELTA` is bounded only by `(0, 7 days]` at construction: [3](#0-2) 

**Attack path:**

1. L2 sequencer goes offline at `T=0`. Last oracle `refTime` = `T - ε` (e.g., 5 minutes before downtime).
2. Market moves significantly (e.g., 5–20%) during downtime.
3. Sequencer restarts at `T = D` where `D < MAX_TIME_DELTA` (e.g., 30 minutes downtime, `MAX_TIME_DELTA = 1 hour`).
4. At restart, `block.timestamp ≈ T + D`. Oracle `refTime` is `T - ε`, so age = `D + ε` < `MAX_TIME_DELTA`. **Staleness check passes.**
5. Attacker calls `pool.swap()` → `ProtectedPriceProviderL2.getBidAndAskPrice()` → `_computeBidAsk()`. The stale pre-downtime bid/ask Q64 values are returned and accepted.
6. Attacker executes at the stale price, draining LP token balances by the price-move delta.

No privileged role is required. The attacker is an ordinary trader calling the public `swap()` entrypoint.

---

### Impact Explanation

**Bad-price execution**: stale, pre-downtime bid/ask Q64 values reach the pool swap. LPs suffer direct loss of principal proportional to the price move during sequencer downtime. This satisfies the "Bad-price execution" and "direct loss of user principal" impact gates.

---

### Likelihood Explanation

L2 sequencer outages are documented historical events (Arbitrum, Optimism, Base have all experienced them). The window of exploitability is the entire period between sequencer restart and the first on-chain oracle update — typically minutes to tens of minutes, which is well within any practical `MAX_TIME_DELTA`. The attack requires no special access, only a funded wallet and knowledge of the restart event (publicly observable on-chain).

---

### Recommendation

Add a Chainlink sequencer uptime feed check with a grace period (the standard is 1 hour) inside `_computeBidAsk` or `getBidAndAskPrice`:

```solidity
address public immutable sequencerUptimeFeed;
uint256 public constant GRACE_PERIOD = 1 hours;

function _checkSequencer() internal view {
    (, int256 answer, uint256 startedAt,,) =
        AggregatorV3Interface(sequencerUptimeFeed).latestRoundData();
    // answer == 0 means sequencer is up
    if (answer != 0) revert SequencerDown();
    if (block.timestamp - startedAt < GRACE_PERIOD) revert GracePeriodNotOver();
}
```

Call `_checkSequencer()` at the top of `_computeBidAsk` before the staleness check. This is the standard Chainlink-recommended pattern for all L2 oracle consumers.

---

### Proof of Concept

```solidity
// Foundry fork test (Arbitrum mainnet fork)
function testStaleSwapAfterSequencerRestart() public {
    // 1. Warp to simulate sequencer downtime: oracle refTime is 30 min old
    uint256 preDowntimeTs = block.timestamp;
    oracle.setData(FEED_ID, 100_000_000, 300, 0, preDowntimeTs); // price = $1.00

    // 2. Simulate 30-min sequencer downtime (market moved to $1.10 off-chain)
    vm.warp(preDowntimeTs + 30 minutes);

    // 3. Sequencer restarts; oracle NOT yet updated (refTime still preDowntimeTs)
    // MAX_TIME_DELTA = 1 hour → staleness check: 30 min < 1 hour → PASSES

    // 4. Attacker swaps at stale $1.00 price
    (uint128 bid, uint128 ask) = provider.getBidAndAskPrice();
    // bid/ask reflect $1.00, not $1.10 — swap succeeds at stale price
    assertGt(bid, 0); // no revert — stale price accepted
}
```

### Citations

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L96-99)
```text
        if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
        if (_futureTolerance > 1 hours) revert FutureToleranceOutOfBounds();
        MAX_TIME_DELTA   = _maxTimeDelta;
        FUTURE_TOLERANCE = _futureTolerance;
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

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L206-209)
```text
        // 1. Staleness check
        if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
            return (0, type(uint128).max);
        }
```
