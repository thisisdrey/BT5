### Title
`confidenceParam` Zero-Initialized in `PriceProvider` / `ProtectedPriceProvider` — Oracle Spread Never Applied, Causing `FeedStalled` or Zero-Spread Execution on Every Newly Deployed Provider - (File: `smart-contracts-poc/contracts/PriceProvider.sol`, `smart-contracts-poc/contracts/ProtectedPriceProvider.sol`)

---

### Summary

Both `PriceProvider.sol` and `ProtectedPriceProvider.sol` store `confidenceParam` as a mutable storage variable that Solidity zero-initializes by default. Neither constructor accepts nor sets a `_confidenceParam` argument. The only way to activate the oracle spread is a post-deployment call to `setConfidenceParam` (factory-gated). No factory code calls this automatically. The result is that every freshly deployed provider operates with `confidenceParam = 0`, collapsing the oracle spread to zero and causing either a `FeedStalled` revert (for prices where `mid × 2^64` is divisible by `1e8`) or a near-zero spread quote (for all other prices) — in both cases the oracle spread component of pool fees is never applied.

---

### Finding Description

**Root cause — constructor never sets `confidenceParam`:**

`PriceProvider.sol` constructor: [1](#0-0) 

`ProtectedPriceProvider.sol` constructor: [2](#0-1) 

Neither constructor has a `_confidenceParam` parameter. `confidenceParam` is a plain storage slot: [3](#0-2) 

**Spread computation with `confidenceParam = 0`:**

In `_getBidAndAskPrice`: [4](#0-3) 

With `confidenceParam = 0`: `adjustedSpread = spread × 0 = 0`. Then in `_getBidAskFrom`: [5](#0-4) 

`delta = 0`, so `bid = ask = mid` (8-decimal). After step adjustment:
- `bidOut = floor(mid × Q64 / 1e8)`
- `askOut = ceil(mid × Q64 / 1e8)`

**Two failure modes:**

1. **`FeedStalled` for round prices** — When `mid × 2^64` is exactly divisible by `1e8` (e.g., `mid = 100_000_000` → $1.00, `mid = 150_000_000` → $1.50, `mid = 200_000_000` → $2.00), `bidOut = askOut`, triggering: [6](#0-5) 
→ returns `(0, type(uint128).max)` → `getBidAndAskPrice()` reverts with `FeedStalled`: [7](#0-6) 

2. **Near-zero spread for non-round prices** — `bidOut` and `askOut` differ by exactly 1 unit (Floor vs Ceil rounding). The pool's `baseFeeX64` (oracle spread fee) is effectively zero.

**`setConfidenceParam` is factory-gated and never called automatically:** [8](#0-7) 

The `AnchoredProviderFactory` pattern (the only factory whose code is fully visible) confirms: `createAnchoredProvider` deploys the provider and registers it, but never calls `setConfidenceParam`: [9](#0-8) 

**Contrast with `AnchoredPriceProvider`** — that contract handles `confidenceParam = 0` safely via its load-bearing band clamp (`min(refBid, cBid)` / `max(refAsk, cAsk)`), so it degenerates gracefully to the band edges. `PriceProvider` and `ProtectedPriceProvider` have no such clamp: [10](#0-9) 

---

### Impact Explanation

- **For round oracle prices** (a large class of real-world prices): every call to `getBidAndAskPrice()` reverts with `FeedStalled`. Any pool using `PriceProvider` or `ProtectedPriceProvider` is completely unusable for swaps — traders cannot execute, and LPs cannot rebalance via the swap path.
- **For non-round prices**: the oracle spread is silently zeroed. The pool's `baseFeeX64` (derived from `midAndSpreadFeeX64FromBidAsk`) is ~0, so the oracle-spread component of LP fees is never collected. Traders receive better-than-intended execution at LP expense.
- Both cases match the allowed impact gate: broken core pool swap functionality and loss of owed LP fee assets.

---

### Likelihood Explanation

Every `PriceProvider` or `ProtectedPriceProvider` deployed without an immediate follow-up `setConfidenceParam` call is affected from block 0. The constructor provides no way to set it at deploy time. The factory pattern does not call it automatically. This is the default operational state for all newly deployed providers.

---

### Recommendation

Add `_confidenceParam` as a constructor parameter and set it at construction time, analogous to how `_marginStep` is validated and stored:

```solidity
constructor(
    address _factory,
    address _oracle,
    bytes32 _offchainFeedId,
    int256  _marginStep,
    uint256 _maxTimeDelta,
    address _baseToken,
    address _quoteToken,
    uint256 _confidenceParam   // ← add this
) {
    ...
    require(_confidenceParam <= CONFIDENCE_MAX, ConfidenceParamOutOfBounds());
    confidenceParam = _confidenceParam;
    lastConfidenceUpdate = block.timestamp;
}
```

Alternatively, if post-deployment tuning is required, the factory's `create*` function must call `setConfidenceParam` immediately after deployment before the provider is attached to any pool.

---

### Proof of Concept

```solidity
// Deploy PriceProvider — confidenceParam is 0 by default
PriceProvider pp = new PriceProvider(
    factory, oracle, feedId, 0, 3600, baseToken, quoteToken
);

// Oracle reports mid = 100_000_000 ($1.00), spread = 300 bps
oracle.setFeed(feedId, 100_000_000, 300, block.timestamp);

// getBidAndAskPrice() reverts FeedStalled — no swap is possible
// because adjustedSpread = 300 * 0 = 0 → bid = ask = mid → bidOut == askOut
pp.getBidAndAskPrice(); // ← reverts FeedStalled

// For a non-round price (mid = 123_456_789):
oracle.setFeed(feedId, 123_456_789, 300, block.timestamp);
(uint128 bid, uint128 ask) = pp.getBidAndAskPrice();
// bid and ask differ by exactly 1 unit — oracle spread is silently zero
// Pool baseFeeX64 ≈ 0 — LP earns no oracle spread fee
```

### Citations

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L40-41)
```text
    uint256 public confidenceParam;
    uint256 public lastConfidenceUpdate;
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L61-89)
```text
    constructor(
        address _factory,
        address _oracle,
        bytes32 _offchainFeedId,
        int256  _marginStep,
        uint256 _maxTimeDelta,
        address _baseToken,
        address _quoteToken
    ) {
        require(_factory != address(0));
        factory = _factory;

        offchainOracle = IOffchainOracle(_oracle);
        offchainFeedId = _offchainFeedId;

        require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
        baseToken = _baseToken;
        quoteToken = _quoteToken;

        if (_marginStep <= -BPS_BASE || _marginStep >= BPS_BASE) {
            revert MarginStepOutOfBounds();
        }
        marginStep       = _marginStep;
        stepBidFactor = uint256(BPS_BASE - _marginStep);
        stepAskFactor = uint256(BPS_BASE + _marginStep);

        if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
        MAX_TIME_DELTA = _maxTimeDelta;
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L92-104)
```text
    function setConfidenceParam(uint256 newValue) external {
        require(msg.sender == factory, OnlyFactory());
        if (newValue > CONFIDENCE_MAX) {
            revert ConfidenceParamOutOfBounds();
        }
        if (block.timestamp < lastConfidenceUpdate + CONFIDENCE_COOLDOWN) {
            revert CooldownNotElapsed();
        }

        confidenceParam = newValue;
        lastConfidenceUpdate = block.timestamp;
        emit ConfidenceParamSet(newValue);
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L115-120)
```text
    function getBidAndAskPrice()
        external override returns (uint128 bid, uint128 ask)
    {
        (bid, ask) = _getBidAndAskPrice();
        if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L137-141)
```text
    function _getBidAskFrom(uint256 midPrice, uint256 confidence) internal pure returns (uint256 bid, uint256 ask) {
        uint256 delta = midPrice * confidence / CONFIDENCE_BASE;
        bid = delta >= midPrice ? 0 : midPrice - delta;
        ask = midPrice + delta;
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L214-218)
```text
        // 5. Compute bid/ask from mid + confidence-adjusted spread
        //    confidenceParam multiplies oracle spread; 0 means no spread
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);

```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L228-228)
```text
        if (bidOut >= askOut) return (0, type(uint128).max);
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProvider.sol (L63-92)
```text
    constructor(
        address _factory,
        address _oracle,
        bytes32 _offchainFeedId,
        int256  _marginStep,
        uint256 _maxTimeDelta,
        address _baseToken,
        address _quoteToken
    ) {
        require(_factory != address(0));
        factory = _factory;

        offchainOracle = IOffchainOracle(_oracle);
        offchainFeedId = _offchainFeedId;

        // Tokens live ONLY here (the oracles are token-free): explicit, mandatory pair.
        require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
        baseToken = _baseToken;
        quoteToken = _quoteToken;

        if (_marginStep <= -BPS_BASE || _marginStep >= BPS_BASE) {
            revert MarginStepOutOfBounds();
        }
        marginStep       = _marginStep;
        stepBidFactor = uint256(BPS_BASE - _marginStep);
        stepAskFactor = uint256(BPS_BASE + _marginStep);

        if (_maxTimeDelta == 0 || _maxTimeDelta > 7 days) revert MaxTimeDeltaOutOfBounds();
        MAX_TIME_DELTA = _maxTimeDelta;
    }
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L182-218)
```text
        AnchoredPriceProvider p = new AnchoredPriceProvider(
            address(this),
            oracle,
            baseFeedId,
            quoteFeedId,
            minMargin,
            maxRefStaleness,
            maxSpreadBps,
            mutableParams,
            marginStep,
            baseToken,
            quoteToken
        );

        provider = address(p);
        address creator = msg.sender;

        _providers.add(provider);
        _providersByCreator[creator].add(provider);
        providerOwner[provider] = creator;

        emit ProviderDeployed(
            provider,
            creator,
            baseFeedId,
            quoteFeedId,
            classId,
            p.baseToken(),
            p.quoteToken(),
            minMargin,
            maxRefStaleness,
            maxSpreadBps,
            mutableParams,
            marginStep,
            oracle
        );
    }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L340-348)
```text
        // 8. Clamp: out-of-band custom quotes are clipped silently to the band edge.
        //    bid ≤ refBid < refAsk ≤ ask, so bid < ask holds by construction.
        uint256 bidOut = Math.min(refBid, cBid);
        uint256 askOut = Math.max(refAsk, cAsk);
        if (bidOut == 0 || bidOut >= askOut) {
            return (0, type(uint128).max);
        }

        return (uint128(bidOut), uint128(askOut));
```
