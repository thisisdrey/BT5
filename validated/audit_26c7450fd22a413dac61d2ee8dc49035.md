Looking at the `AnchoredPriceProvider` synthetic-ratio path and `AnchoredProviderFactory.createAnchoredProvider()`, I found a valid analog.

---

### Title
`baseFeedId == quoteFeedId` Not Validated in `AnchoredPriceProvider` Collapses Synthetic Price to 1.0, Enabling Bad-Price Execution Against Pool LPs — (`smart-contracts-poc/contracts/AnchoredPriceProvider.sol`, `smart-contracts-poc/contracts/AnchoredProviderFactory.sol`)

---

### Summary

`AnchoredPriceProvider` supports a two-feed synthetic-ratio mode: when `quoteFeedId != bytes32(0)`, it computes `mid = price(baseFeedId) / price(quoteFeedId)`. Neither the provider constructor nor the permissionless `AnchoredProviderFactory.createAnchoredProvider()` validates that `baseFeedId != quoteFeedId`. When both are set to the same non-zero feed ID, the division always yields `ORACLE_DECIMALS = 1e8` (i.e., a synthetic price of exactly 1.0), regardless of the actual market price. The resulting provider passes `isProvider()` and, if assigned to a pool, causes every swap to execute at price 1.0 instead of the true market price — draining LP principal.

---

### Finding Description

**Root cause — missing identity check in the factory (permissionless path):**

`AnchoredProviderFactory.createAnchoredProvider()` is callable by anyone, subject only to the oracle being on the admin allow-list and the numeric parameters fitting the envelope: [1](#0-0) 

There is no `require(baseFeedId != quoteFeedId)` guard anywhere in this function or in the `AnchoredPriceProvider` constructor: [2](#0-1) 

**Corrupted value — synthetic price collapses to 1.0:**

In `_getBidAndAskPrice()`, when `quoteFeedId != bytes32(0)` the two-leg path is taken: [3](#0-2) 

When `baseFeedId == quoteFeedId`, both `_readLeg` calls query the same oracle feed and return the same `mid` value. The ratio computation then becomes:

```
mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2)
    = Math.mulDiv(X, 1e8, X)
    = 1e8   // always, for any non-zero X
``` [4](#0-3) 

The resulting `mid = 1e8` (price = 1.0 in 8-decimal terms) is then passed to `_computeBidAsk`, which produces a valid, non-stall bid/ask pair centered on 1.0. The `FeedStalled` sentinel `(0, type(uint128).max)` is **not** returned — the pool receives a well-formed but completely wrong price.

**Provider passes eligibility check:**

The bad provider is registered in `_providers` by the factory and `isProvider()` returns `true`: [5](#0-4) [6](#0-5) 

**Bad price reaches pool swap:**

The pool calls `getBidAndAskPrice()` at swap time and uses the returned bid/ask to compute `midPriceX64` and `baseFeeX64` for all bin math: [7](#0-6) [8](#0-7) 

---

### Impact Explanation

If the real market price is, for example, BTC/ETH = 20 (BTC worth 20 ETH), but the provider returns price ≈ 1.0, a trader can buy 1 BTC for ≈ 1 ETH instead of 20 ETH — a ~95% discount. Repeating this drains all token0 (or token1) from the pool at near-zero cost to the attacker, constituting direct loss of LP principal. The `_computeBidAsk` band clamp does not protect against this because the band itself is computed from the corrupted `mid = 1e8`, so the clamp only enforces that the final quote is no tighter than the already-wrong band.

---

### Likelihood Explanation

`createAnchoredProvider` is permissionless — any address can call it once an admin has approved at least one oracle and configured an envelope (both of which are expected normal operations). The attacker supplies `baseFeedId == quoteFeedId` with any valid feed ID known to the approved oracle. No special role, no front-running, no flash loan is required. The only prerequisite is that the resulting provider is assigned to a pool; if pool creation is permissionless (the architecture describes `isProvider()` as the machine-checkable eligibility predicate), the entire attack chain is unprivileged.

---

### Recommendation

Add an identity check in both the `AnchoredPriceProvider` constructor and `AnchoredProviderFactory.createAnchoredProvider()`:

```solidity
// In AnchoredPriceProvider constructor (after storing baseFeedId/quoteFeedId):
require(
    _quoteFeedId == bytes32(0) || _baseFeedId != _quoteFeedId,
    "baseFeedId == quoteFeedId"
);
```

```solidity
// In AnchoredProviderFactory.createAnchoredProvider():
require(
    quoteFeedId == bytes32(0) || baseFeedId != quoteFeedId,
    "baseFeedId == quoteFeedId"
);
```

This mirrors the existing `_baseToken != _quoteToken` guard already present in the constructor for tokens: [9](#0-8) 

---

### Proof of Concept

1. Admin approves oracle `O` (via `addOracle`) and sets an envelope for feed class of feed `F`.
2. Attacker calls:
   ```solidity
   anchoredProviderFactory.createAnchoredProvider(
       oracle:          O,
       baseFeedId:      F,   // e.g., BTC/USD feed
       quoteFeedId:     F,   // same feed — no revert
       minMargin:       validValue,
       maxRefStaleness: validValue,
       maxSpreadBps:    validValue,
       mutableParams:   false,
       marginStep:      0,
       baseToken:       BTC_ADDRESS,
       quoteToken:      ETH_ADDRESS
   );
   ```
3. Provider `P` is deployed. `isProvider(P) == true`.
4. A pool is created (or updated) with provider `P`.
5. Oracle `O` reports BTC/USD = 50 000e8. Both `_readLeg(F)` calls return `mid = 50000e8`.
6. `_getBidAndAskPrice()` computes `mid = mulDiv(50000e8, 1e8, 50000e8) = 1e8`.
7. Pool prices BTC at 1 ETH. Attacker swaps 1 ETH → 1 BTC (fair value: 20 ETH). LP loses 19 ETH worth of BTC per swap. [3](#0-2) [10](#0-9)

### Citations

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L156-218)
```text
    function createAnchoredProvider(
        address oracle,
        bytes32 baseFeedId,
        bytes32 quoteFeedId,
        uint256 minMargin,
        uint256 maxRefStaleness,
        uint16  maxSpreadBps,
        bool    mutableParams,
        int256  marginStep,
        address baseToken,
        address quoteToken
    ) external override returns (address provider) {
        if (!_oracles.contains(oracle)) revert OracleNotAllowed(oracle);

        // Feeds without an explicit class fall back to the admin-configured DEFAULT_CLASS envelope.
        bytes32 classId = feedClass[baseFeedId];
        if (classId == bytes32(0)) classId = DEFAULT_CLASS;

        Envelope storage env = envelopes[classId];
        if (!env.exists) revert EnvelopeNotFound(classId);
        if (
            minMargin < env.minMarginMin || minMargin > env.minMarginMax
            || maxRefStaleness < env.stalenessMin || maxRefStaleness > env.stalenessMax
            || maxSpreadBps < env.maxSpreadMin || maxSpreadBps > env.maxSpreadMax
        ) revert ParamsOutOfEnvelope();

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

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L281-283)
```text
    function isProvider(address provider) external view returns (bool) {
        return _providers.contains(provider);
    }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L123-172)
```text
    constructor(
        address _factory,
        address _oracle,
        bytes32 _baseFeedId,
        bytes32 _quoteFeedId,
        uint256 _minMargin,
        uint256 _maxRefStaleness,
        uint16  _maxSpreadBps,
        bool    _mutableParams,
        int256  _marginStep,
        address _baseToken,
        address _quoteToken
    ) {
        require(_factory != address(0));
        factory = _factory;

        offchainOracle = IOffchainOracle(_oracle);
        baseFeedId = _baseFeedId;
        quoteFeedId = _quoteFeedId;

        // Tokens live ONLY here (the oracles are token-free): the pair is an explicit,
        // mandatory input — including the synthetic (two-feed) mode, where the factory
        // knows the pair when it creates the pool.
        require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
        baseToken = _baseToken;
        quoteToken = _quoteToken;

        if (_maxRefStaleness > 7 days) revert MaxRefStalenessOutOfBounds(); // 0 allowed = same-block reference
        MAX_REF_STALENESS = _maxRefStaleness;

        if (_maxSpreadBps == 0 || _maxSpreadBps >= ORACLE_BPS) revert MaxSpreadOutOfBounds();
        MAX_SPREAD_BPS = _maxSpreadBps;

        // minMargin 0 is allowed: the band then relies purely on the oracle spreadBps. If spreadBps is
        // also 0 the band degenerates and the read halts via the refBid >= refAsk guard in _computeBidAsk
        // (never a tighter-than-band quote) — the clamp + that halt are the safety net, not a positive floor.
        // Worst-case half-width must stay below 100% so the clamped bid is always positive.
        if (uint256(_maxSpreadBps) * ONE_BPS_E18 + _minMargin >= BPS_BASE_U) revert BandTooWide();
        minMargin = _minMargin;

        MUTABLE_PARAMS = _mutableParams;
        // marginStep bias + derived step factors (immutable). The customizable variant shapes the quote
        // with confidence then this fixed bias; the load-bearing band clamp in _computeBidAsk keeps the
        // final quote no tighter than the band edge for ANY marginStep sign (a negative value tightens or
        // inverts the pre-clamp quote; the clamp neutralizes it). The immutable variant ignores them.
        if (_marginStep <= -BPS_BASE || _marginStep >= BPS_BASE) revert MarginStepOutOfBounds();
        marginStep = _marginStep;
        stepBidFactor = uint256(BPS_BASE - _marginStep);
        stepAskFactor = uint256(BPS_BASE + _marginStep);
    }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L258-272)
```text
    function _getBidAndAskPrice() internal returns (uint128, uint128) {
        (uint256 mid, uint256 spreadBps, , bool ok) = _readLeg(baseFeedId);
        if (!ok) return (0, type(uint128).max);

        bytes32 _quote = quoteFeedId;
        if (_quote != bytes32(0)) {
            (uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLeg(_quote);
            if (!ok2 || mid2 == 0) return (0, type(uint128).max);
            // Synthetic ratio (8-decimal): mid1 / mid2. Relative uncertainties of a ratio add.
            mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);
            spreadBps += spreadBps2;
        }

        return _computeBidAsk(mid, spreadBps);
    }
```

**File:** metric-core/contracts/MetricOmmPool.sol (L242-248)
```text
    (uint256 midPriceX64, uint256 baseFeeX64) =
      SwapMath.midAndSpreadFeeX64FromBidAsk(uint256(bidPriceX64), uint256(askPriceX64));
    SwapMath.InternalSwapParams memory params =
      SwapMath.InternalSwapParams({midPriceX64: midPriceX64, baseFeeX64: baseFeeX64, priceLimitX64: priceLimitX64});

    (int256 amount0Delta, int256 amount1Delta, uint256 protocolFeeAmount) =
      _executeSwap(zeroForOne, amountSpecified, params);
```

**File:** metric-core/contracts/MetricOmmPool.sol (L804-813)
```text
  function _getBidAndAskPriceX64() internal returns (uint128 bidPriceX64, uint128 askPriceX64) {
    address activePriceProvider = _resolvedPriceProvider();
    try IPriceProvider(activePriceProvider).getBidAndAskPrice() returns (uint128 bid, uint128 ask) {
      if (bid >= ask) revert BidGreaterThanAsk();
      if (bid == 0) revert BidIsZero();
      return (bid, ask);
    } catch (bytes memory reason) {
      revert PriceProviderFailed(reason);
    }
  }
```
