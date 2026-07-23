### Title
Self-Referential Feed in Synthetic Mode Locks Price at 1.0 Regardless of Market — (`smart-contracts-poc/contracts/AnchoredProviderFactory.sol`)

### Summary

`AnchoredProviderFactory.createAnchoredProvider` accepts `baseFeedId == quoteFeedId` without any validation. When both feed IDs are identical, the synthetic ratio `price(baseFeedId) / price(quoteFeedId)` always collapses to exactly `1e8` (1.0 in 8-decimal), regardless of actual market prices. This is the direct structural analog to the FRAX bug: just as FRAX counted FRAX as collateral (self-referential asset), a provider with `baseFeedId == quoteFeedId` divides the same oracle reading by itself, producing a constant ratio of 1.0 that is then forwarded to pool swaps as a valid bid/ask.

### Finding Description

In `AnchoredPriceProvider._getBidAndAskPrice()`, the synthetic path is:

```solidity
(uint256 mid, uint256 spreadBps, , bool ok) = _readLeg(baseFeedId);
...
bytes32 _quote = quoteFeedId;
if (_quote != bytes32(0)) {
    (uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLeg(_quote);
    if (!ok2 || mid2 == 0) return (0, type(uint128).max);
    mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);   // mid1 / mid2
    spreadBps += spreadBps2;
}
return _computeBidAsk(mid, spreadBps);
``` [1](#0-0) 

When `baseFeedId == quoteFeedId`, both `_readLeg` calls hit the same oracle slot and return identical values `mid1 == mid2 == X`. The division `Math.mulDiv(X, 1e8, X)` yields exactly `1e8` for any non-zero `X`. The resulting bid/ask from `_computeBidAsk(1e8, 2*spreadBps)` is always centered on Q64 (price = 1.0), regardless of the actual market price of the token pair.

Neither the `AnchoredPriceProvider` constructor nor `AnchoredProviderFactory.createAnchoredProvider` validates `baseFeedId != quoteFeedId`:

```solidity
// AnchoredPriceProvider constructor — no check on feed IDs being distinct
baseFeedId = _baseFeedId;
quoteFeedId = _quoteFeedId;
require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
``` [2](#0-1) 

The factory's envelope validation only gates `minMargin`, `maxRefStaleness`, and `maxSpreadBps` — it never inspects the feed IDs:

```solidity
if (
    minMargin < env.minMarginMin || minMargin > env.minMarginMax
    || maxRefStaleness < env.stalenessMin || maxRefStaleness > env.stalenessMax
    || maxSpreadBps < env.maxSpreadMin || maxSpreadBps > env.maxSpreadMax
) revert ParamsOutOfEnvelope();
``` [3](#0-2) 

The factory's documented guarantee is: `recognizedFactory.isProvider(p)` is the machine-checkable predicate for public-pool eligibility. [4](#0-3) 

A provider with `baseFeedId == quoteFeedId` passes `isProvider()` as `true`, falsely signalling safety to any downstream consumer of that predicate.

The `MetricOmmPoolFactory._validatePriceProvider` only checks that `token0()`/`token1()` match the pool tokens — it does not inspect feed IDs:

```solidity
function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
        revert PriceProviderTokenMismatch();
    }
}
``` [5](#0-4) 

So a pool created with a self-referential provider passes all factory validation and is deployed as a live pool.

### Impact Explanation

Any pool whose provider has `baseFeedId == quoteFeedId` quotes a fixed price of 1.0 (Q64) for every swap, regardless of the actual market price of the token pair. For a pool such as ETH/USDC where the true price is 3000:1, the pool's bid/ask is anchored at 1:1. A swapper can buy ETH at 1 USDC each, draining all LP-deposited ETH. LP principal is directly and completely lost. This satisfies the "bad-price execution" and "pool insolvency" impact categories.

### Likelihood Explanation

`createAnchoredProvider` is permissionless for any caller using an admin-approved oracle. The only constraint is that the oracle must be in the allow-list; the feed IDs are freely chosen by the caller. The factory's `isProvider` predicate returns `true` for the resulting provider, and `MetricOmmPoolFactory.createPool` accepts it without further feed-level validation. Any caller can deploy this provider and pool without any privileged role.

### Recommendation

Add a check in both `AnchoredProviderFactory.createAnchoredProvider` and the `AnchoredPriceProvider` constructor:

```solidity
require(quoteFeedId == bytes32(0) || quoteFeedId != baseFeedId, "Self-referential feed");
```

This mirrors the existing `_baseToken != _quoteToken` guard and closes the analogous gap for feed IDs. [6](#0-5) 

### Proof of Concept

```
bytes32 feedId = keccak256("ETH/USD");

// 1. Permissionless: create a self-referential provider
address provider = anchoredFactory.createAnchoredProvider(
    approvedOracle,
    feedId,      // baseFeedId
    feedId,      // quoteFeedId — same feed, self-referential
    minMargin, maxRefStaleness, maxSpreadBps,
    false, 0,
    ETH_ADDRESS, USDC_ADDRESS   // token0 != token1 passes constructor check
);
// anchoredFactory.isProvider(provider) == true  ← false safety signal

// 2. Permissionless: create a pool with this provider
// _validatePriceProvider passes: provider.token0()==ETH, provider.token1()==USDC
address pool = poolFactory.createPool(PoolParameters({
    token0: ETH_ADDRESS, token1: USDC_ADDRESS,
    priceProvider: provider, ...
}));

// 3. LPs add liquidity at market price (1 ETH + 3000 USDC)

// 4. Oracle: ETH/USD = 3000e8. Both _readLeg calls return mid=3000e8.
//    mid = mulDiv(3000e8, 1e8, 3000e8) = 1e8  → price = 1.0 in Q64
//    Pool quotes: buy 1 ETH for 1 USDC

// 5. Attacker swaps 1 USDC → receives 1 ETH (worth 3000 USDC)
//    Repeat until pool is drained.
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

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L176-180)
```text
        if (
            minMargin < env.minMarginMin || minMargin > env.minMarginMax
            || maxRefStaleness < env.stalenessMin || maxRefStaleness > env.stalenessMax
            || maxSpreadBps < env.maxSpreadMin || maxSpreadBps > env.maxSpreadMax
        ) revert ParamsOutOfEnvelope();
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
