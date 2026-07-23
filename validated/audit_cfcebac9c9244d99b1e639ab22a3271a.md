### Title
Pool Swaps Break After Price Provider Update Due to Missing Oracle Registration Check — (`metric-core/contracts/MetricOmmPoolFactory.sol`)

---

### Summary

`executePoolPriceProviderUpdate` validates only that the new provider's `token0()`/`token1()` match the pool's tokens, but does not verify that the pool is registered for the new provider's `baseFeedId` (and `quoteFeedId` in synthetic mode) in the oracle. After the update, every swap call reaches `oracle.price(newFeedId, pool)`, which reverts `NotRegistered(newFeedId, pool)`, making the pool's swap flow entirely unusable until a separate permissionless registration step is completed.

---

### Finding Description

**Root cause — `executePoolPriceProviderUpdate` in `MetricOmmPoolFactory.sol`:** [1](#0-0) 

The function calls `_validatePriceProvider`, which only checks token-pair identity: [2](#0-1) 

It does **not** check `oracle.registeredPool[newFeedId][pool]`.

**Swap-time failure path:**

After the update, `MetricOmmPool.swap` calls `_getBidAndAskPriceX64`, which calls `newProvider.getBidAndAskPrice()`: [3](#0-2) 

Inside `AnchoredPriceProvider._readLeg`, the oracle is called with the new provider's immutable `baseFeedId`: [4](#0-3) 

The oracle's `price` function enforces: [5](#0-4) 

`registeredPool[newFeedId][pool]` is `false` (the pool was registered for the **old** provider's feedId, not the new one), so the oracle reverts `NotRegistered(newFeedId, pool)`. The pool's `try/catch` re-reverts with `PriceProviderFailed`. Every swap fails.

For synthetic-ratio providers (`quoteFeedId != bytes32(0)`), the pool must be registered for **both** `baseFeedId` and `quoteFeedId`: [6](#0-5) 

Missing either registration breaks the swap.

---

### Impact Explanation

All swap calls revert with `PriceProviderFailed` after the provider update. The pool's core swap flow is unusable. LP positions are not at risk (liquidity add/remove does not call the price provider), but no trading can occur. This matches the "unusable swap/liquidity flows" criterion in the impact gate.

---

### Likelihood Explanation

The pool admin is a semi-trusted role that legitimately calls `proposePoolPriceProvider` + `executePoolPriceProviderUpdate` to migrate to a new oracle or feed. The registration step (`oracle.register(newFeedId, pool, factory)`) is a separate, permissionless call that is easy to overlook. The factory provides no guard or hint that it is required. Any provider migration to a new feedId or a new oracle contract triggers the DoS.

---

### Recommendation

In `executePoolPriceProviderUpdate`, before calling `pool.setPriceProvider(pending)`, verify that the pool is already registered for the new provider's feedId(s) in the oracle, or revert with a descriptive error:

```solidity
// pseudo-code addition inside executePoolPriceProviderUpdate
IOffchainOracle oracle = IOffchainOracle(IPriceProvider(pending).offchainOracle());
bytes32 base = IPriceProvider(pending).baseFeedId();
bytes32 quote = IPriceProvider(pending).quoteFeedId();
if (!oracle.registeredPool(base, pool)) revert PoolNotRegisteredForNewFeed(base, pool);
if (quote != bytes32(0) && !oracle.registeredPool(quote, pool))
    revert PoolNotRegisteredForNewFeed(quote, pool);
```

Alternatively, document the required pre-condition prominently and emit a warning event.

---

### Proof of Concept

1. Pool is deployed with provider `P1` using `feedId1`; `oracle.registeredPool[feedId1][pool] = true`.
2. Pool admin calls `proposePoolPriceProvider(pool, P2)` where `P2` uses `feedId2` (same or different oracle). `_validatePriceProvider` passes (token0/token1 match).
3. Timelock elapses; pool admin calls `executePoolPriceProviderUpdate(pool)`. Succeeds — no registration check.
4. `pool.priceProvider` is now `P2`.
5. Any user calls `pool.swap(...)`:
   - `_getBidAndAskPriceX64()` → `P2.getBidAndAskPrice()` → `oracle.price(feedId2, pool)`
   - Oracle: `registeredPool[feedId2][pool] == false` → reverts `NotRegistered(feedId2, pool)`
   - Pool: `catch` → reverts `PriceProviderFailed(...)`
6. All swaps revert. The pool's swap flow is unusable until `oracle.register(feedId2, pool, factory)` is called separately. [7](#0-6) [5](#0-4) [8](#0-7)

### Citations

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L474-507)
```text
  function proposePoolPriceProvider(address pool, address newPriceProvider)
    external
    override
    nonReentrant
    onlyPoolAdmin(pool)
  {
    PoolImmutables memory p = IMetricOmmPool(pool).getImmutables();
    uint256 timelock = priceProviderTimelock[pool];
    if (p.immutablePriceProvider != address(0)) revert PriceProviderImmutable();
    _validatePriceProvider(p.token0, p.token1, newPriceProvider);

    address mutableProvider = PoolStateLibrary._slot3(pool);
    address current = mutableProvider != address(0) ? mutableProvider : p.immutablePriceProvider;
    uint256 executeAfter = block.timestamp + timelock;
    pendingPriceProvider[pool] = newPriceProvider;
    pendingPriceProviderExecuteAfter[pool] = executeAfter;
    emit PoolPriceProviderChangeProposed(pool, current, newPriceProvider, executeAfter);
  }

  /// @inheritdoc IMetricOmmPoolFactoryPoolAdmin
  function executePoolPriceProviderUpdate(address pool) external override nonReentrant onlyPoolAdmin(pool) {
    address pending = pendingPriceProvider[pool];
    if (pending == address(0)) revert NoPriceProviderChangeProposed();
    uint256 execAfter = pendingPriceProviderExecuteAfter[pool];
    // forge-lint: disable-next-line(block-timestamp) -- timelock enforcement legitimately relies on `block.timestamp`.
    if (block.timestamp < execAfter) revert PriceProviderTimelockNotElapsed(execAfter, block.timestamp);
    PoolImmutables memory p = IMetricOmmPool(pool).getImmutables();
    if (p.immutablePriceProvider != address(0)) revert PriceProviderImmutable();
    _validatePriceProvider(p.token0, p.token1, pending);
    IMetricOmmPoolFactoryActions(pool).setPriceProvider(pending);
    delete pendingPriceProvider[pool];
    delete pendingPriceProviderExecuteAfter[pool];
    emit PoolPriceProviderUpdated(pool, pending);
  }
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

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L64-70)
```text
    // ── Immutables ──────────────────────────────────────────────────────
    IOffchainOracle public immutable offchainOracle;
    bytes32         public immutable baseFeedId;
    /// @notice Optional second feed for synthetic ratio quoting; zero = single-feed (no conversion).
    ///         Synthetic mid = price(baseFeedId) / price(quoteFeedId), e.g. BTC/USD ÷ ETH/USD = BTC/ETH.
    bytes32         public immutable quoteFeedId;
    /// @dev anchor factory (governs setSource), NOT the AMM pool factory passed at read.
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
