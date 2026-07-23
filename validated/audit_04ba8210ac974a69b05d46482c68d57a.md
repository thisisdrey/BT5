### Title
Pool using `AnchoredPriceProvider` in synthetic mode is permanently DoS'd when `quoteFeedId` registration is absent — (`smart-contracts-poc/contracts/AnchoredPriceProvider.sol`)

---

### Summary

`AnchoredPriceProvider` supports a **synthetic ratio mode** (`quoteFeedId != bytes32(0)`) that reads two oracle feeds and divides them (e.g. BTC/USD ÷ ETH/USD = BTC/ETH). Every on-chain oracle read requires the pool to be registered for the specific `feedId` via `oracle.register(feedId, pool, factory)`. In synthetic mode the pool must therefore be registered for **both** `baseFeedId` and `quoteFeedId`. Neither `AnchoredProviderFactory.createAnchoredProvider` nor `MetricOmmPoolFactory.createPool` enforces or even checks this dual-registration requirement. If the pool is registered only for `baseFeedId`, every swap reverts with `NotRegistered(quoteFeedId, pool)`, making the pool permanently unusable for trading.

---

### Finding Description

**Vulnerable path — `AnchoredPriceProvider._getBidAndAskPrice`:**

```solidity
// AnchoredPriceProvider.sol L258-L271
function _getBidAndAskPrice() internal returns (uint128, uint128) {
    (uint256 mid, uint256 spreadBps, , bool ok) = _readLeg(baseFeedId);
    if (!ok) return (0, type(uint128).max);

    bytes32 _quote = quoteFeedId;
    if (_quote != bytes32(0)) {
        (uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLeg(_quote);   // ← second feed read
        if (!ok2 || mid2 == 0) return (0, type(uint128).max);
        mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);
        spreadBps += spreadBps2;
    }
    return _computeBidAsk(mid, spreadBps);
}
```

`_readLeg` calls `oracle.price(feedId, msg.sender)` with **no try/catch**:

```solidity
// AnchoredPriceProvider.sol L280
(mid, spreadBps, , refTime) = IPricedOracle(address(offchainOracle)).price(feedId, msg.sender);
```

The oracle enforces registration unconditionally:

```solidity
// OracleBase.sol L168
require(registeredPool[feedId][pool], NotRegistered(feedId, pool));
``` [1](#0-0) [2](#0-1) [3](#0-2) 

**Factory gap — `AnchoredProviderFactory.createAnchoredProvider`:**

The factory validates the envelope only against `baseFeedId`. The `quoteFeedId` receives zero validation — no existence check, no envelope check, and no registration requirement:

```solidity
// AnchoredProviderFactory.sol L171-L172
bytes32 classId = feedClass[baseFeedId];   // only baseFeedId is class-checked
if (classId == bytes32(0)) classId = DEFAULT_CLASS;
```

The NatDoc comment claims `quoteFeedId` is "validated for existence at provider construction," but the `AnchoredPriceProvider` constructor stores it without any oracle call:

```solidity
// AnchoredPriceProvider.sol L141
quoteFeedId = _quoteFeedId;   // no existence or registration check
``` [4](#0-3) [5](#0-4) 

**Pool factory gap — `MetricOmmPoolFactory.createPool`:**

`createPool` only validates that `priceProvider.token0()/token1()` match the pool tokens. It performs no oracle registration and does not check whether the pool is registered for any feed:

```solidity
// MetricOmmPoolFactory.sol L541-L545
function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1)
        revert PriceProviderTokenMismatch();
}
``` [6](#0-5) 

Registration is a separate, manual, permissionless call (`oracle.register(feedId, pool, factory)`). In synthetic mode, two such calls are required — one per feed — but nothing in the deployment flow enforces or documents this. [7](#0-6) 

---

### Impact Explanation

Every swap in the pool calls `getBidAndAskPrice()`, which calls `_getBidAndAskPrice()`, which calls `_readLeg(quoteFeedId)`, which calls `oracle.price(quoteFeedId, pool)`. Because there is no try/catch, the `NotRegistered` revert propagates all the way up and the swap reverts. The pool is completely unusable for trading. LPs who deposited liquidity cannot earn fees; the pool's core function is broken. This matches the allowed impact: **broken core pool functionality causing unusable swap flows**.

---

### Likelihood Explanation

Any pool deployer who creates a synthetic-ratio pool and registers only for `baseFeedId` (the natural, single-step action) triggers this condition. The factory provides no guard, the NatDoc is actively misleading ("validated for existence at provider construction"), and the integration documentation describes registration as a single `register` call. The probability that a deployer of a synthetic pool omits the second registration is non-trivial, especially given the misleading comment.

---

### Recommendation

1. **`AnchoredProviderFactory.createAnchoredProvider`**: when `quoteFeedId != bytes32(0)`, call `IOffchainOracle(_oracle).getOracleData(quoteFeedId)` (or equivalent existence check) and revert if the feed is unknown. Remove the false NatDoc claim.
2. **`AnchoredPriceProvider` constructor**: if `quoteFeedId != bytes32(0)`, verify the feed exists in the oracle at construction time.
3. **Documentation / deployment scripts**: explicitly state that synthetic-mode pools must call `oracle.register` for **both** `baseFeedId` and `quoteFeedId` before the first swap.

---

### Proof of Concept

```
1. Deploy AnchoredPriceProvider(oracle, baseFeedId=BTC_USD, quoteFeedId=ETH_USD, ...)
2. Deploy MetricOmmPool with this provider via MetricOmmPoolFactory.createPool(...)
3. oracle.register{value:1}(BTC_USD, pool, factory)   // register for baseFeedId only
   // quoteFeedId (ETH_USD) is NOT registered
4. LP calls modifyLiquidity / addLiquidity → succeeds (no price read at this stage)
5. Trader calls pool.swap(...)
   → pool calls getBidAndAskPrice()
   → AnchoredPriceProvider._readLeg(ETH_USD)
   → oracle.price(ETH_USD, pool)
   → require(registeredPool[ETH_USD][pool])  ← FALSE
   → revert NotRegistered(ETH_USD, pool)
6. All swaps revert. Pool is permanently broken for trading until
   someone separately calls oracle.register{value:1}(ETH_USD, pool, factory).
```

### Citations

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L139-148)
```text
        offchainOracle = IOffchainOracle(_oracle);
        baseFeedId = _baseFeedId;
        quoteFeedId = _quoteFeedId;

        // Tokens live ONLY here (the oracles are token-free): the pair is an explicit,
        // mandatory input — including the synthetic (two-feed) mode, where the factory
        // knows the pair when it creates the pool.
        require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
        baseToken = _baseToken;
        quoteToken = _quoteToken;
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L258-271)
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
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L277-295)
```text
    function _readLeg(bytes32 feedId)
        internal returns (uint256 mid, uint256 spreadBps, uint256 refTime, bool ok)
    {
        (mid, spreadBps, , refTime) = IPricedOracle(address(offchainOracle)).price(feedId, msg.sender);

        // Stale reference → not ok. Clamping to a stale anchor is the one false-safety case.
        if (_isStale(refTime, block.timestamp, MAX_REF_STALENESS)) return (mid, spreadBps, refTime, false);

        // Basic validity — mid positive, spreadBps not the stalled/off-hours marker (the Chainlink oracle
        // writes spreadBps = ORACLE_BPS when an RWA market is closed).
        if (mid == 0 || spreadBps >= ORACLE_BPS) return (mid, spreadBps, refTime, false);

        // Per-leg price guard.
        (uint128 guardMin, uint128 guardMax) = offchainOracle.priceGuard(feedId);
        guardMax = guardMax == 0 ? type(uint128).max : guardMax;
        if (mid < guardMin || mid > guardMax) return (mid, spreadBps, refTime, false);

        ok = true;
    }
```

**File:** smart-contracts-poc/contracts/oracles/providers/OracleBase.sol (L160-172)
```text
    function price(bytes32 feedId, address pool)
        external
        feedExists(feedId)
        notBlacklisted
        returns (uint256 mid, uint256 spread, uint16 spread1, uint256 refTime)
    {
        require(pool != address(0) && IPool(pool).inSwap() == msg.sender, InvalidInSwap());
        require(!blacklisted[pool], Blacklisted(pool));
        require(registeredPool[feedId][pool], NotRegistered(feedId, pool));

        (mid, spread, spread1, refTime) = _readPrice(feedId);
        emit PriceRead(pool, feedId);
    }
```

**File:** smart-contracts-poc/contracts/oracles/providers/OracleBase.sol (L201-214)
```text
    function register(bytes32 feedId, address pool, address factory) external payable {
        require(msg.value >= registrationFee, InsufficientFee(msg.value, registrationFee));
        require(pool != address(0));
        require(approvedFactories.contains(factory), FactoryNotApproved(factory));
        require(IPoolFactory(factory).isPool(pool), NotAPool(pool));

        if (blacklisted[pool]) {
            blacklisted[pool] = false;
            emit BlacklistUpdated(pool, false);
        }

        registeredPool[feedId][pool] = true;
        emit PoolRegistered(feedId, pool, msg.sender, msg.value);
    }
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L153-194)
```text
    /// @param quoteFeedId optional second feed for synthetic ratio quoting (zero = single-feed). The
    ///        envelope is keyed on `baseFeedId` (the provider's class); the ref feed only contributes its
    ///        uncertainty and is validated for existence at provider construction.
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
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L541-546)
```text
  function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
      revert PriceProviderTokenMismatch();
    }
  }
```
