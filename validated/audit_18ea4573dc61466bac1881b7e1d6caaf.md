### Title
Pool Admin Can Bind an Unvalidated Oracle via Permissionless `PriceProviderFactory`, Delivering Manipulated Bid/Ask to Pool Swaps — (`metric-core/contracts/MetricOmmPoolFactory.sol`, `smart-contracts-poc/contracts/PriceProviderFactory.sol`)

---

### Summary

`MetricOmmPoolFactory._validatePriceProvider` only checks that the provider's `token0()`/`token1()` match the pool's tokens. It does not verify that the provider was deployed by a factory with oracle validation (e.g., `AnchoredProviderFactory`). `PriceProviderFactory.createPriceProvider` is fully permissionless and accepts any arbitrary oracle address. A pool admin can therefore create a `PriceProvider` backed by a malicious oracle, propose it as the pool's price provider, and — if the pool's `priceProviderTimelock` is zero — execute the swap in the same block. Every subsequent swap then prices against the attacker-controlled oracle.

---

### Finding Description

**Step 1 — Permissionless provider creation with arbitrary oracle.**

`PriceProviderFactory.createPriceProvider` accepts any `_oracle` address with no allow-list check: [1](#0-0) 

Contrast this with `AnchoredProviderFactory.createAnchoredProvider`, which gates on an admin-curated oracle allow-list: [2](#0-1) 

**Step 2 — Factory validation only checks token-pair identity.**

`_validatePriceProvider` is the sole gate used both at pool creation and at every price-provider update: [3](#0-2) 

It calls `token0()` / `token1()` on the provider — both are set by the `PriceProvider` constructor from caller-supplied arguments — so any provider with the right token pair passes, regardless of which oracle backs it.

**Step 3 — Pool admin proposes and executes the malicious provider.** [4](#0-3) 

When `priceProviderTimelock[pool] == 0`, `executeAfter = block.timestamp`, and the `block.timestamp < execAfter` guard is immediately false, so propose + execute can occur in a single transaction.

**Step 4 — Malicious oracle bypasses every `PriceProvider` guard.**

Inside `PriceProvider._getBidAndAskPrice`, the oracle itself supplies the `priceGuard` bounds: [5](#0-4) 

A malicious oracle returns `guardMin = 0`, `guardMax = type(uint128).max`, `refTime = block.timestamp`, and any `mid` / `spread` it chooses. All four guards (staleness, zero-mid, spread-marker, price-guard) pass. The resulting bid/ask — computed from the attacker-chosen `mid` — flows directly into the pool swap: [6](#0-5) 

**Step 5 — Wrong bid/ask drives swap math.**

`swap()` calls `_getBidAndAskPriceX64()`, derives `midPriceX64` and `baseFeeX64` from the attacker-controlled bid/ask, and passes them into `_executeSwap`: [7](#0-6) 

Every bin price boundary (`lowerPriceX64`, `upperPriceX64`) is computed relative to this corrupted `midPriceX64`: [8](#0-7) 

---

### Impact Explanation

If the malicious oracle reports `mid` at 1/N of the true market price, a trader buying token0 with token1 receives N× more token0 than the pool's LP reserves can sustain at fair value. LP principal is drained proportionally. Because the pool is oracle-anchored with no internal reserve-ratio backstop, there is no self-correcting mechanism once the price provider is swapped. The loss is direct and immediate on the first swap after the update.

---

### Likelihood Explanation

Requires a malicious pool admin — a semi-trusted role analogous to governance in the referenced report. The pool admin is set at pool creation and can be any address. Pools created with `priceProviderTimelock = 0` (a valid and likely common configuration for pools that want operational flexibility) have no delay between proposal and execution, eliminating the only user-protection window. Likelihood is Medium, matching the external report's severity rating for the same class of admin rug vector.

---

### Recommendation

`_validatePriceProvider` should additionally verify that the provider was deployed by a recognized factory with oracle validation:

```solidity
function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
        revert PriceProviderTokenMismatch();
    }
    // Add: require the provider to be recognized by an approved factory
    if (!IAnchoredProviderFactory(anchoredProviderFactory).isProvider(priceProvider)) {
        revert UnrecognizedPriceProvider();
    }
}
```

Alternatively, enforce a non-zero minimum `priceProviderTimelock` so that users always have a reaction window before a malicious provider becomes active.

---

### Proof of Concept

```solidity
// 1. Deploy malicious oracle: returns mid = 1e6 (1/1000 of true price),
//    refTime = block.timestamp, spread = 1, guardMin = 0, guardMax = type(uint128).max.
MaliciousOracle oracle = new MaliciousOracle(token0, token1);

// 2. Create a PriceProvider backed by the malicious oracle (permissionless).
address badProvider = PriceProviderFactory(ppFactory).createPriceProvider(
    address(oracle), feedId, 0 /*marginStep*/, 1 days, token0, token1
);
// badProvider.token0() == token0, badProvider.token1() == token1 → passes _validatePriceProvider

// 3. Pool admin proposes the malicious provider (pool has priceProviderTimelock = 0).
factory.proposePoolPriceProvider(pool, badProvider);   // executeAfter = block.timestamp

// 4. Execute immediately in the same block.
factory.executePoolPriceProviderUpdate(pool);
// pool.priceProvider = badProvider

// 5. Attacker swaps: buys token0 at 1/1000 of market price, draining LP reserves.
router.swap(pool, zeroForOne=false, amountSpecified, ...);
```

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L41-57)
```text
    function createPriceProvider(
        address _oracle,
        bytes32 _feedId,
        int256  _marginStep,
        uint256 _maxTimeDelta,
        address _baseToken,
        address _quoteToken
    ) external override returns (address provider) {
        PriceProvider p = new PriceProvider(
            address(this),
            _oracle,
            _feedId,
            _marginStep,
            _maxTimeDelta,
            _baseToken,
            _quoteToken
        );
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L168-168)
```text
        if (!_oracles.contains(oracle)) revert OracleNotAllowed(oracle);
```

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

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L207-212)
```text
        // 4. Price guard check (moved from oracle)
        (uint128 guardMin, uint128 guardMax) = offchainOracle.priceGuard(offchainFeedId);
        guardMax = guardMax == 0 ? type(uint128).max : guardMax;
        if (mid < guardMin || mid > guardMax) {
            return (0, type(uint128).max);
        }
```

**File:** metric-core/contracts/MetricOmmPool.sol (L228-248)
```text
    (uint128 bidPriceX64, uint128 askPriceX64) = _getBidAndAskPriceX64();

    _beforeSwap(
      msg.sender,
      recipient,
      zeroForOne,
      amountSpecified,
      priceLimitX64,
      packedSlot0Initial,
      bidPriceX64,
      askPriceX64,
      extensionData
    );

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

**File:** metric-core/contracts/MetricOmmPool.sol (L850-851)
```text
    lowerPriceX64 = distanceE6ToPriceX64(curBinDistE6Cache, params.midPriceX64);
    upperPriceX64 = distanceE6ToPriceX64(_addDistE6(curBinDistE6Cache, binState.lengthE6), params.midPriceX64);
```
