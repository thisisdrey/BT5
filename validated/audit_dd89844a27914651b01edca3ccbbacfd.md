### Title
L2 Price Providers Lack Sequencer Uptime Check, Enabling Stale-Price Swaps After Sequencer Recovery — (`smart-contracts-poc/contracts/PriceProviderL2.sol` and `smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol`)

---

### Summary

`PriceProviderL2` and `ProtectedPriceProviderL2` are the designated L2 price providers for the Metric OMM pool, deployed on Arbitrum, Avalanche, Base, Berachain, and BSC. Neither contract checks the Chainlink L2 sequencer uptime feed. When the sequencer goes offline and recovers, the oracle's `refTime` may still fall within `MAX_TIME_DELTA`, causing `_isStale` to pass and allowing pool swaps to execute at prices that predate the outage.

---

### Finding Description

Both L2 providers implement a staleness guard via `_isStale`:

```solidity
// PriceProviderL2._getBidAndAskPrice (line 215)
if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
    return (0, type(uint128).max);
}
``` [1](#0-0) 

`_isStale` only checks whether the oracle's `refTime` is older than `MAX_TIME_DELTA` (up to 7 days by construction): [2](#0-1) 

`ProtectedPriceProviderL2._computeBidAsk` applies the identical guard: [3](#0-2) 

Neither contract stores nor queries a `sequencerUptimeFeed`. The constructors accept no such parameter: [4](#0-3) [5](#0-4) 

The codebase does contain a `ChainlinkVerifierL2` oracle backend (visible in the registry ABI at lines 5685–5791) that stores a `sequencerUptimeFeed` and enforces a `GRACE_PERIOD`. However, that check lives inside the Chainlink oracle backend only. When either L2 provider is paired with a Pyth-based oracle (`CompressedOracleV1`, `PythOracle`), no sequencer uptime check exists anywhere in the call path:

```
pool.swap
  → PriceProviderL2.getBidAndAskPrice
      → IPricedOracle(pythOracle).price(feedId, pool)   // no sequencer check
      → _isStale(refTime, ...)                          // only checks age, not uptime
      → bid/ask returned to pool
```

`PriceProviderFactoryL2.createPriceProvider` also accepts no sequencer uptime feed, so every provider it deploys inherits the gap: [6](#0-5) 

---

### Impact Explanation

**Bad-price execution reaching pool swaps.** Scenario:

1. Sequencer goes offline on Arbitrum/Base/etc. for duration `D < MAX_TIME_DELTA`.
2. Oracle data is frozen at the pre-outage price `P_old`.
3. Sequencer recovers. `block.timestamp` has advanced by `D`; oracle `refTime` is `D` seconds old.
4. `_isStale` passes because `D < MAX_TIME_DELTA`.
5. A trader immediately calls `pool.swap` before oracle keepers push a fresh update.
6. The pool settles the trade at `P_old`, which may differ materially from the true market price `P_now`.

The pool receives the wrong bid/ask from the provider and executes swap math against it. LPs bear the loss: a trader buying the underpriced token drains LP inventory at a below-market rate, or sells the overpriced token into the pool above market. The magnitude scales with `D × price_volatility` and is bounded only by `MAX_TIME_DELTA` (up to 7 days).

---

### Likelihood Explanation

L2 sequencer outages are documented historical events on Arbitrum, Optimism, and Base. The protocol is explicitly deployed on multiple L2 chains (registry shows Arbitrum, Avalanche, Base, Berachain, BSC). Any outage shorter than `MAX_TIME_DELTA` creates the exploitable window. A sophisticated trader monitoring sequencer status can time the swap to execute in the first block after recovery, before oracle keepers update the feed. No privileged access is required — only a standard `pool.swap` call.

---

### Recommendation

Add a sequencer uptime check to `PriceProviderL2` and `ProtectedPriceProviderL2`, mirroring the pattern already present in `ChainlinkVerifierL2`. Concretely:

1. Accept an immutable `AggregatorV3Interface sequencerUptimeFeed` in the constructor (zero address = L1/no check).
2. In `_getBidAndAskPrice` / `_computeBidAsk`, before the staleness check, call `sequencerUptimeFeed.latestRoundData()`, assert `answer == 0` (sequencer is up), and assert `block.timestamp - startedAt >= GRACE_PERIOD` (e.g., 3600 seconds).
3. If either assertion fails, return `(0, type(uint128).max)` to halt the swap.
4. Update `PriceProviderFactoryL2.createPriceProvider` to accept and forward the `sequencerUptimeFeed` address.

---

### Proof of Concept

```solidity
// Foundry test sketch
function testStaleSwapAfterSequencerRecovery() public {
    // 1. Set oracle price at T=0
    uint64 priceBeforeOutage = 2000_00000000; // $2000
    oracle.setFeed(FEED_ID, priceBeforeOutage, 100, block.timestamp);

    // 2. Simulate sequencer outage: advance time by 4 hours (< MAX_TIME_DELTA = 1 day)
    vm.warp(block.timestamp + 4 hours);
    // Oracle NOT updated during outage — refTime is still T=0

    // 3. Sequencer recovers. Oracle keepers haven't pushed yet.
    // _isStale: nowTs - refTime = 4h < MAX_TIME_DELTA = 1 day → passes
    // True market price is now $1800 (moved during outage), but provider returns $2000

    (uint128 bid, uint128 ask) = provider.getBidAndAskPrice();
    // bid/ask reflect $2000, not $1800
    // Pool swap executes at stale $2000 price — trader profits, LPs lose
    assertGt(bid, 0, "stale price accepted");
}
```

The test demonstrates that `getBidAndAskPrice` returns a non-zero (accepted) quote using the pre-outage price, with no sequencer uptime gate blocking the swap.

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L64-96)
```text
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

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L68-100)
```text
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

**File:** smart-contracts-poc/contracts/ProtectedPriceProviderL2.sol (L206-209)
```text
        // 1. Staleness check
        if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
            return (0, type(uint128).max);
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
