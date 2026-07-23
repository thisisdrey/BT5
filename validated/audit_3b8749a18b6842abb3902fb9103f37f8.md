### Title
`AnchoredPriceProvider` synthetic-ratio always collapses to 1.0 when `baseFeedId == quoteFeedId`, delivering a permanently wrong mid price to every pool swap — (`smart-contracts-poc/contracts/AnchoredPriceProvider.sol`, `smart-contracts-poc/contracts/AnchoredProviderFactory.sol`)

---

### Summary

`AnchoredProviderFactory.createAnchoredProvider` is permissionless and accepts any `(baseFeedId, quoteFeedId)` pair without checking that the two feed IDs differ. When `baseFeedId == quoteFeedId != bytes32(0)`, the synthetic-ratio path inside `AnchoredPriceProvider._getBidAndAskPrice` divides the feed's own price by itself, producing a hard-coded mid of exactly `1e8` (= 1.0 in 8-decimal) for every call. The factory's `isProvider()` returns `true` for the resulting contract, making it appear eligible for public pools. Any pool that uses such a provider executes every swap at a price of ~1.0 regardless of the real market rate, enabling complete LP-fund drainage.

---

### Finding Description

**Root cause — no `baseFeedId != quoteFeedId` guard anywhere in the creation path.**

`AnchoredProviderFactory.createAnchoredProvider` is `external` with no role restriction. Its only feed-level validation is an envelope check keyed on `baseFeedId`; it never compares the two feed IDs:

```solidity
// AnchoredProviderFactory.sol  lines 156-194
function createAnchoredProvider(
    address oracle,
    bytes32 baseFeedId,
    bytes32 quoteFeedId,   // ← accepted verbatim, never compared to baseFeedId
    ...
) external override returns (address provider) {
    if (!_oracles.contains(oracle)) revert OracleNotAllowed(oracle);
    ...
    AnchoredPriceProvider p = new AnchoredPriceProvider(
        address(this), oracle,
        baseFeedId, quoteFeedId,   // ← forwarded as-is
        ...
    );
```

The `AnchoredPriceProvider` constructor likewise stores both IDs without comparing them:

```solidity
// AnchoredPriceProvider.sol  lines 139-141
offchainOracle = IOffchainOracle(_oracle);
baseFeedId = _baseFeedId;
quoteFeedId = _quoteFeedId;   // no require(_baseFeedId != _quoteFeedId)
```

**How the wrong price is produced.**

`_getBidAndAskPrice` enters the synthetic-ratio branch whenever `quoteFeedId != bytes32(0)`:

```solidity
// AnchoredPriceProvider.sol  lines 258-271
function _getBidAndAskPrice() internal returns (uint128, uint128) {
    (uint256 mid, uint256 spreadBps, , bool ok) = _readLeg(baseFeedId);
    if (!ok) return (0, type(uint128).max);

    bytes32 _quote = quoteFeedId;
    if (_quote != bytes32(0)) {
        (uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLeg(_quote);
        if (!ok2 || mid2 == 0) return (0, type(uint128).max);
        mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);   // ← mid / mid2
        spreadBps += spreadBps2;
    }
    return _computeBidAsk(mid, spreadBps);
}
```

When `baseFeedId == quoteFeedId`, both `_readLeg` calls query the **same feed** and return the same value `x`. The ratio is:

```
mid = Math.mulDiv(x, 1e8, x) = 1e8   (exactly 1.0 in 8-decimal, always)
```

`_computeBidAsk` then builds bid/ask around this fixed mid of `1e8`, completely independent of the real market price. The doubled `spreadBps` widens the band but does not correct the mid.

**Why the factory's safety predicate is broken.**

The factory's stated purpose is that `isProvider(p)` is the "machine-checkable predicate" for public-pool eligibility. A misconfigured provider created with `baseFeedId == quoteFeedId` is added to `_providers` and `isProvider()` returns `true` for it, giving it a false certificate of safety.

---

### Impact Explanation

A pool whose `PRICE_PROVIDER` is set to such a misconfigured `AnchoredPriceProvider` will call `getBidAndAskPrice()` on every swap and receive bid/ask prices centered on `1.0` (in 8-decimal, converted to Q64.64 for the pool). For any real-world pair where the true price differs from 1.0 — e.g., ETH/USDC at 3 000, BTC/ETH at 20, etc. — every swap executes at the wrong price:

- Traders buying the base token pay ~1 quote unit instead of the market rate, draining the pool's base reserves.
- LPs suffer direct, unrecoverable loss of principal proportional to the price deviation.

This satisfies the "bad-price execution" and "pool insolvency" impact gates: the corrupted bid/ask reaches the pool swap path and LP claims can no longer be covered.

---

### Likelihood Explanation

`createAnchoredProvider` is **permissionless** — any EOA can call it with `baseFeedId == quoteFeedId` as long as the oracle is in the admin allow-list (a public, enumerable set). The resulting provider passes `isProvider()`, the only on-chain eligibility check a pool admin would consult. A pool admin acting in good faith who verifies `isProvider(p) == true` before configuring the pool would be misled. No privileged role, no malicious setup assumption, and no non-standard token behavior is required.

---

### Recommendation

Add a guard in both the factory and the constructor:

**`AnchoredProviderFactory.createAnchoredProvider`** (before deploying the provider):
```solidity
if (quoteFeedId != bytes32(0) && quoteFeedId == baseFeedId)
    revert SameFeedId();
```

**`AnchoredPriceProvider` constructor** (defense-in-depth):
```solidity
require(
    _quoteFeedId == bytes32(0) || _quoteFeedId != _baseFeedId,
    "same feed"
);
```

---

### Proof of Concept

1. Admin has added oracle `O` to the allow-list and configured a DEFAULT_CLASS envelope.
2. Attacker calls:
   ```solidity
   factory.createAnchoredProvider(
       O,
       FEED_ETH_USD,   // baseFeedId
       FEED_ETH_USD,   // quoteFeedId  ← same as baseFeedId
       minMargin, maxRefStaleness, maxSpreadBps,
       false, 0,
       ETH, USDC
   );
   ```
3. Factory deploys `AnchoredPriceProvider` with `baseFeedId == quoteFeedId == FEED_ETH_USD`. `isProvider(provider) == true`.
4. Pool admin (honest, checks `isProvider`) sets this as the pool's price provider.
5. Oracle reports ETH/USD = 3 000e8. Both `_readLeg(FEED_ETH_USD)` calls return `mid = 3000e8`.
6. `mid = Math.mulDiv(3000e8, 1e8, 3000e8) = 1e8` — provider quotes ETH/USDC ≈ 1.0.
7. Trader calls `swap(exactIn: 1 USDC → ETH)`. Pool prices the swap at ~1.0 USDC/ETH and delivers ~1 ETH to the trader instead of ~0.000333 ETH.
8. Pool's ETH balance is drained; LP claims are insolvent.