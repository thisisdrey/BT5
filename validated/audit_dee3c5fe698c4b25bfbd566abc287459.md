Looking at the repository structure and key contracts, I need to trace the bid/ask price path from oracle → provider → pool swap, and check for sequencing or binding failures analogous to the Astaria `buyoutLien`-before-`transferWithdrawReserve` pattern.

**Anchored quote path analysis:**

`AnchoredPriceProvider._computeBidAsk` applies the band clamp correctly: [1](#0-0) 

The clamp guarantees `bidOut ≤ refBid < refAsk ≤ askOut`, so a source can only widen the spread — never tighten it or invert it. The `_readSource` guard rejects zero bid, inverted, or overflowing source output before the clamp is reached. [2](#0-1) 

**Factory path analysis:**

`MetricOmmPoolFactory._validatePriceProvider` only checks token matching — it does not verify factory origin or `isProvider()`: [3](#0-2) 

However, using any price provider in a pool requires `onlyPoolAdmin(pool)` for both `proposePoolPriceProvider` and `executePoolPriceProviderUpdate`: [4](#0-3) 

**Sequencing analysis (fee collection before state changes):**

Both `setPoolAdminFees` and `setPoolProtocolFee` correctly call `collectFees` before updating fee parameters: [5](#0-4) 

`setPoolBinAdditionalFees` does not call `collectFees

### Citations

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L342-348)
```text
        uint256 bidOut = Math.min(refBid, cBid);
        uint256 askOut = Math.max(refAsk, cAsk);
        if (bidOut == 0 || bidOut >= askOut) {
            return (0, type(uint128).max);
        }

        return (uint128(bidOut), uint128(askOut));
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L404-410)
```text
        if (!success || retSize != 64) return (false, 0, 0);

        srcBid = b;
        srcAsk = a;
        if (srcBid == 0 || srcBid >= srcAsk || srcAsk > type(uint128).max) return (false, 0, 0);

        return (true, srcBid, srcAsk);
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L417-432)
```text
    PoolFeeConfig memory c = poolFeeConfig[pool];
    IMetricOmmPoolCollectFees(pool)
      .collectFees(
        c.protocolSpreadFeeE6,
        c.adminSpreadFeeE6,
        c.protocolNotionalFeeE8,
        c.adminNotionalFeeE8,
        poolAdminFeeDestination[pool]
      );

    c.adminSpreadFeeE6 = newAdminSpreadFeeE6;
    c.adminNotionalFeeE8 = newAdminNotionalFeeE8;
    poolFeeConfig[pool] = c;

    IMetricOmmPoolFactoryActions(pool)
      .setPoolFees(c.protocolSpreadFeeE6 + c.adminSpreadFeeE6, c.protocolNotionalFeeE8 + c.adminNotionalFeeE8);
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L474-491)
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
