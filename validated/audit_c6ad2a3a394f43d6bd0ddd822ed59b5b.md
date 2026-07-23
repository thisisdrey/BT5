After reading all the relevant price provider contracts, I can now analyze whether a valid analog exists.

**Key observation**: `PriceProviderL2` and `ProtectedPriceProviderL2` are explicitly L2-targeted contracts (they carry `FUTURE_TOLERANCE` to handle sequencer clock skew), yet neither queries a Chainlink L2 sequencer uptime feed. The only guard against stale prices is the `MAX_TIME_DELTA` staleness check, which only catches prices older than the configured window — not prices that became stale because the sequencer went offline for less than that window.

---

### Title
Missing L2 Sequencer Uptime Check in L2 Price Providers Allows Stale Bid/Ask Prices to Reach Pool Swaps — (`smart-contracts-poc/contracts/PriceProviderL2.sol`, `smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

### Summary
`PriceProviderL2` and `ProtectedPriceProviderL2` include L2-specific logic (`FUTURE_TOLERANCE`) to tolerate sequencer clock skew, but neither checks whether the L2 sequencer is actually online. When the sequencer is offline, oracle prices freeze at their last pre-outage value. If the outage is shorter than `MAX_TIME_DELTA`, the staleness check passes and the frozen price is served as a valid bid/ask to the pool swap.

### Finding Description

Both L2 providers call `IPricedOracle.price(feedId, pool)` and then run `_isStale()`:

`PriceProviderL2._isStale()` (lines 135–150):
```solidity
function _isStale(
    uint256 refTime, uint256 nowTs,
    uint256 maxDelta, uint256 futureTol
) internal pure returns (bool) {
    if (refTime == 0) return true;
    if (refTime > nowTs) {
        return (refTime - nowTs) > futureTol;   // clock-skew tolerance only
    }
    return (nowTs - refTime) > maxDelta;
}
``` [1](#0-0) 

The identical pattern appears in `ProtectedPriceProviderL2._isStale()`: [2](#0-1) 

`FUTURE_TOLERANCE` (capped at 1 hour at construction) is designed to absorb sequencer clock skew — it is **not** a sequencer liveness check: [3](#0-2) 

`MAX_TIME_DELTA` is configurable up to 7 days: [4](#0-3) 

`PriceProviderFactoryL2.createPriceProvider` is **permissionless** — any caller can deploy a provider with any `MAX_TIME_DELTA` up to 7 days: [5](#0-4) 

The resulting `getBidAndAskPrice()` call reverts with `FeedStalled` only if the sentinel `(0, max)` is returned — which only happens when `refTime` is older than `MAX_TIME_DELTA`. A sequencer outage shorter than `MAX_TIME_DELTA` produces no sentinel: [6](#0-5) 

There is no Chainlink L2 sequencer uptime feed queried anywhere in either L2 provider.

### Impact Explanation

When the L2 sequencer goes offline, the on-chain oracle cannot receive new price updates (L2 transactions are not processed). The last pre-outage price remains in oracle storage. If the outage is shorter than `MAX_TIME_DELTA`, `_isStale()` returns `false`, the frozen price passes all guards (staleness, price guard, spread check), and a valid bid/ask pair is returned to the pool. Any swap executed during or immediately after the outage (via L1 force-include or on sequencer resumption before the oracle is refreshed) settles at the stale price. If the real market price moved significantly during the outage (e.g., a de-peg), LPs absorb the difference — a direct loss of principal.

### Likelihood Explanation

Arbitrum and other L2 sequencers have experienced documented outages. The providers are explicitly L2-targeted (the `FUTURE_TOLERANCE` immutable exists for no other reason). `PriceProviderFactoryL2.createPriceProvider` is permissionless, so providers with long `MAX_TIME_DELTA` values (hours to days) are deployable by anyone. No privileged access is required to trigger the impact: a user submits a swap via the L1 delayed inbox during sequencer downtime, or immediately after resumption before the oracle is refreshed.

### Recommendation

Add a Chainlink L2 sequencer uptime feed check inside `_getBidAndAskPrice()` in both `PriceProviderL2` and `ProtectedPriceProviderL2`. If the sequencer is reported down, or if it came back online within a configurable grace period (e.g., 1 hour), return the stall sentinel `(0, type(uint128).max)` so `getBidAndAskPrice()` reverts with `FeedStalled`. The sequencer feed address should be an immutable set at construction (and validated by the factory). Example pattern:

```solidity
(, int256 answer, uint256 startedAt, ,) = sequencerFeed.latestRoundData();
if (answer != 0 || block.timestamp - startedAt < GRACE_PERIOD) {
    return (0, type(uint128).max);
}
```

### Proof of Concept

1. Deploy `PriceProviderL2` via `PriceProviderFactoryL2` with `MAX_TIME_DELTA = 3600` (1 hour) and `FUTURE_TOLERANCE = 60`.
2. Oracle records `refTime = T`, `mid = 1e8` (USDC = $1.00).
3. At `T + 5 minutes`, the L2 sequencer goes offline. Oracle cannot update.
4. USDC de-pegs to $0.90 on L1; oracle `mid` remains `1e8`.
5. At `T + 30 minutes`, a user submits a swap via the Arbitrum L1 delayed inbox (force-include).
6. `_isStale(T, T+1800, 3600, 60)` → `(1800 > 3600)` → `false`. Staleness check passes.
7. `mid = 1e8`, `spread` within bounds, price guard passes → valid bid/ask returned.
8. Pool swap executes at the stale $1.00 price; LPs receive USDC worth $0.90 per unit, suffering a ~10% loss on the swapped notional.

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

**File:** smart-contracts-poc/contracts/PriceProviderFactoryL2.sol (L41-79)
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

        provider = address(p);
        address creator = msg.sender;

        _providers.add(provider);
        _providersByCreator[creator].add(provider);
        providerOwner[provider] = creator;

        emit ProviderDeployed(
            provider,
            creator,
            _feedId,
            _oracle,
            p.baseToken(),
            p.quoteToken(),
            _marginStep,
            _maxTimeDelta,
            _futureTolerance
        );
    }
```
