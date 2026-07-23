### Title
`PriceProviderL2` and `ProtectedPriceProviderL2` lack L2 sequencer uptime / grace-period check, allowing pool swaps to execute against pre-downtime stale prices immediately after sequencer restart — (`smart-contracts-poc/contracts/PriceProviderL2.sol`, `smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

---

### Summary

Both L2 price providers (`PriceProviderL2` and `ProtectedPriceProviderL2`) are deployed on Arbitrum, Base, Avalanche, BSC, and Berachain (per the contract registry). They guard against stale oracle data only via a `MAX_TIME_DELTA` age check on the oracle's `refTime`. Neither contract checks the Chainlink L2 sequencer uptime feed or enforces a post-restart grace period. When the L2 sequencer goes offline and then comes back up, oracle data that was published just before the outage may still be within `MAX_TIME_DELTA`, pass all staleness guards, and be fed directly into pool swaps — even though the price it encodes is from before the downtime and may be far from the current market price.

---

### Finding Description

**Invariant broken**: Every bid/ask quote reaching a pool swap must reflect a price that was observable while the L2 sequencer was continuously live. A price published before a sequencer outage is not a valid anchor for a swap executed after the outage.

**Root cause — `PriceProviderL2._getBidAndAskPrice()`**:

```solidity
// PriceProviderL2.sol L208-L217
function _getBidAndAskPrice() internal returns (uint128, uint128) {
    (uint256 mid, uint256 spread, , uint256 refTime) =
        IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);

    // 2. Staleness check
    if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
        return (0, type(uint128).max);
    }
    ...
```

The only temporal guard is `_isStale`, which accepts any `refTime` that is no older than `MAX_TIME_DELTA` (up to 7 days). There is no call to a Chainlink sequencer uptime feed and no grace-period enforcement.

**Identical root cause in `ProtectedPriceProviderL2._computeBidAsk()`**:

```solidity
// ProtectedPriceProviderL2.sol L206-L208
if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
    return (0, type(uint128).max);
}
```

**Attack scenario**:

1. The L2 sequencer goes offline. Oracle data on L2 stops updating.
2. The last oracle update (published just before the outage) has `refTime = T_last`. If `MAX_TIME_DELTA` is, say, 2 hours, and the outage lasts 90 minutes, then `T_last` is still within the staleness window when the sequencer restarts.
3. The sequencer comes back online. The oracle has not yet been updated (or the first update is still propagating). `block.timestamp` is now `T_last + 90 min`.
4. A trader calls `swap()` on the pool. The pool calls `getBidAndAskPrice()`. `_isStale` passes because `block.timestamp - refTime = 90 min < MAX_TIME_DELTA = 2 hours`.
5. The bid/ask returned is computed from the pre-outage price. If the market moved 5% during the outage, the pool quotes a price that is 5% away from fair value.
6. The trader (or a bot watching the sequencer restart) immediately arbitrages the pool, extracting value from LPs.

The protocol itself acknowledges the need for sequencer uptime checks: the contract registry contains a `ChainlinkVerifierL2` contract with `sequencerUptimeFeed` and `GRACE_PERIOD` fields, and the registry ABI for the L2 factory variant includes a `_sequencerUptimeFeed` constructor parameter. Neither `PriceProviderL2` nor `ProtectedPriceProviderL2` wires this verifier into their pricing path. [1](#0-0) [2](#0-1) [3](#0-2) 

---

### Impact Explanation

**Bad-price execution reaching pool swaps**: A swap that executes against a pre-outage price can give the trader a better-than-fair rate, directly extracting value from the pool's LP reserves. The magnitude of the loss equals the price drift during the outage multiplied by the swap volume. For a 2-hour outage on a volatile asset, a 5–10% price move is plausible, and the pool has no mechanism to reject the swap or widen the spread to compensate.

This satisfies the "bad-price execution: stale, inverted, unbounded, or unclamped bid/ask quote reaches a pool swap" impact gate. [4](#0-3) 

---

### Likelihood Explanation

- Both providers are deployed on multiple L2 chains (Arbitrum, Base, Avalanche, BSC, Berachain).
- L2 sequencer outages are documented historical events (Arbitrum had a ~7-hour outage in 2022; Base and others have had shorter incidents).
- The exploit requires no privileged access: any EOA can call `swap()` on the pool immediately after the sequencer restarts.
- The window of vulnerability is bounded by `MAX_TIME_DELTA` minus the outage duration, which can be hours.
- MEV bots routinely monitor sequencer restarts for exactly this class of arbitrage. [5](#0-4) 

---

### Recommendation

Add a sequencer uptime check (using the Chainlink L2 sequencer feed) and enforce a grace period before accepting oracle data after a restart. The pattern already exists in the codebase's `ChainlinkVerifierL2` component (visible in the contract registry). Wire it into `_getBidAndAskPrice()` / `_computeBidAsk()`:

```solidity
// Pseudocode — add to _getBidAndAskPrice() before the staleness check
if (address(sequencerUptimeFeed) != address(0)) {
    (, int256 answer, uint256 startedAt, , ) =
        sequencerUptimeFeed.latestRoundData();
    // answer == 0 means sequencer is up; 1 means down
    if (answer != 0) return (0, type(uint128).max);          // sequencer down
    if (block.timestamp - startedAt < GRACE_PERIOD)          // just restarted
        return (0, type(uint128).max);
}
```

`GRACE_PERIOD` should be at least 3600 seconds (Chainlink's recommended value). The `sequencerUptimeFeed` address should be an immutable set at construction, with `address(0)` meaning "L1 / no check needed" (preserving backward compatibility with `PriceProvider.sol`). [6](#0-5) 

---

### Proof of Concept

```
Setup:
  - Deploy PriceProviderL2 on Arbitrum with MAX_TIME_DELTA = 7200 (2 hours)
  - Oracle last updated at T=0 with price = 1000 USDC/ETH

Scenario:
  T=0:    Oracle publishes price=1000, refTime=0
  T=100:  L2 sequencer goes offline
  T=5500: L2 sequencer comes back online (90-minute outage)
          Real market price is now 1050 USDC/ETH (5% move)
          Oracle has NOT yet been updated on L2

  Attacker calls pool.swap() at T=5500:
    pool calls getBidAndAskPrice()
    _isStale(refTime=0, nowTs=5500, maxDelta=7200, futureTol=...) 
      → (5500 - 0) = 5500 < 7200 → NOT stale → passes
    bid/ask computed from price=1000 (pre-outage)
    Attacker buys ETH at 1000 USDC when market is 1050 USDC
    Attacker immediately sells on CEX at 1050 USDC
    Profit: 50 USDC per ETH extracted from LP reserves

Result: LPs suffer a 5% loss on the swapped volume with no recourse.
``` [3](#0-2) [7](#0-6)

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L34-38)
```text
    uint256 public immutable MAX_TIME_DELTA;

    /// @dev L2 sequencer timestamp can lag behind oracle publication time.
    ///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
    uint256 public immutable FUTURE_TOLERANCE;
```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L63-96)
```text
    // ── Constructor ─────────────────────────────────────────────────────
    constructor(
        address _factory,
        address _oracle,
        bytes32 _offchainFeedId,
        int256  _marginStep,
        uint256 _maxTimeDelta,
        uint256 _futureTolerance,
        address _baseToken,
        address _quoteToken
    ) {
        require(_factory != address(0));
        factory = _factory;

        offchainOracle = IOffchainOracle(_oracle);
        offchainFeedId = _offchainFeedId;

        // Tokens live ONLY here (the oracles are token-free): explicit, mandatory pair.
        require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
        baseToken  = _baseToken;
        quoteToken = _quoteToken;

        if (_marginStep <= -BPS_BASE || _marginStep >= BPS_BASE) {
            revert MarginStepOutOfBounds();
        }
        marginStep       = _marginStep;
        stepBidFactor = uint256(BPS_BASE - _marginStep);
        stepAskFactor = uint256(BPS_BASE + _marginStep);

        if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
        if (_futureTolerance > 1 hours) revert FutureToleranceOutOfBounds();
        MAX_TIME_DELTA   = _maxTimeDelta;
        FUTURE_TOLERANCE = _futureTolerance;
    }
```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L123-128)
```text
    function getBidAndAskPrice()
        external override returns (uint128 bid, uint128 ask)
    {
        (bid, ask) = _getBidAndAskPrice();
        if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
    }
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

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L135-153)
```text
    // ── Staleness ───────────────────────────────────────────────────────

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

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L202-209)
```text
    /// @dev Downstream pricing: staleness, price guard, confidence spread, marginStep.
    function _computeBidAsk(uint256 price, uint256 spread, uint256 refTime)
        internal view returns (uint128, uint128)
    {
        // 1. Staleness check
        if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
            return (0, type(uint128).max);
        }
```
