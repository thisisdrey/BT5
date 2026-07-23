After thoroughly reading `PriceProviderFactory.sol`, `AnchoredProviderFactory.sol`, `AnchoredPriceProvider.sol`, `PriceProvider.sol`, and `ProtectedPriceProvider.sol`, here is my analysis:

---

### Title
Stale `isUpdater` Grants Persist After `removeProvider`, Allowing Unauthorized `confidenceParam` Manipulation on Re-Added Providers — (`smart-contracts-poc/contracts/PriceProviderFactory.sol`)

### Summary
`PriceProviderFactory.removeProvider` removes a provider from the tracked set but does not clear `providerOwner[provider]` or any `isUpdater[provider][updater]` entries. If the same provider is later re-added via `addProvider`, all previously revoked updaters silently regain the ability to call `setConfidence`, manipulating the `confidenceParam` that directly shapes the bid/ask spread delivered to pool swaps.

### Finding Description
`removeProvider` performs only two state mutations:

```solidity
// PriceProviderFactory.sol lines 118-127
function removeProvider(address provider) external override onlyRole(ADMIN_ROLE) {
    if (!_providers.remove(provider)) revert ProviderNotTracked();

    address owner = providerOwner[provider];
    if (owner != address(0)) {
        _providersByCreator[owner].remove(provider);
    }

    emit ProviderRemoved(provider);
}
```

It does **not** clear:
- `providerOwner[provider]` — old owner mapping persists
- `isUpdater[provider][*]` — all updater grants persist

When the admin later calls `addProvider` for the same address, the function reads the stale `providerOwner` and re-registers the provider under the old owner:

```solidity
// PriceProviderFactory.sol lines 106-116
function addProvider(address provider) external override onlyRole(ADMIN_ROLE) {
    require(PriceProvider(provider).factory() == address(this));
    if (!_providers.add(provider)) revert ProviderAlreadyTracked();

    address owner = providerOwner[provider];
    if (owner != address(0)) {
        _providersByCreator[owner].add(provider);  // stale owner re-registered
    }
    emit ProviderAdded(provider);
}
```

`setConfidence` gates access via `_requireUpdater`, which checks the stale `isUpdater` mapping:

```solidity
// PriceProviderFactory.sol lines 34-37
function _requireUpdater(address provider) internal view {
    if (msg.sender != providerOwner[provider] && !isUpdater[provider][msg.sender])
        revert NotProviderUpdater();
}
```

Because `isUpdater[provider][oldUpdater]` was never cleared, the old updater passes this check and can call `setConfidence` on the re-added provider, setting `confidenceParam` to any value in `[0, CONFIDENCE_MAX]`.

### Impact Explanation
`confidenceParam` directly controls the oracle spread applied to the mid price before the bid/ask is computed and returned to the pool swap:

```solidity
// PriceProvider.sol lines 216-217
uint256 adjustedSpread = spread * confidenceParam;
(uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);
```

- **Setting `confidenceParam = 0`** collapses `adjustedSpread` to zero. With `marginStep = 0`, `bidOut == askOut`, triggering the `bidOut >= askOut` guard and returning the stall sentinel `(0, type(uint128).max)`, causing `FeedStalled` on every swap — a complete swap DoS.
- **Setting `confidenceParam = 0` with `marginStep > 0`** removes the oracle confidence buffer entirely, leaving only the fixed `marginStep` spread. The pool quotes prices tighter than the oracle's own uncertainty warrants, exposing LPs to adverse selection / LVR on every subsequent swap.
- **Setting `confidenceParam = CONFIDENCE_MAX`** maximally widens the spread, degrading swap execution quality for all traders.

### Likelihood Explanation
The trigger requires the admin to remove and then re-add the same provider address — a plausible operational sequence (e.g., temporary delisting for maintenance, then reinstatement). The admin has no on-chain signal that stale updater grants persist; the `ProviderAdded` event carries no updater inventory. Any previously granted updater who was supposed to lose access after removal retains it silently.

### Recommendation
In `removeProvider`, explicitly clear ownership and all updater grants before removing from the set, or at minimum document that re-adding a removed provider reactivates all prior grants and require a fresh `createPriceProvider` deployment instead of `addProvider` for reinstatement. Concretely:

```solidity
function removeProvider(address provider) external override onlyRole(ADMIN_ROLE) {
    if (!_providers.remove(provider)) revert ProviderNotTracked();

    address owner = providerOwner[provider];
    if (owner != address(0)) {
        _providersByCreator[owner].remove(provider);
        delete providerOwner[provider];   // ← add
    }
    // isUpdater entries cannot be enumerated without an additional data structure;
    // the safest fix is to prohibit re-adding removed providers via addProvider,
    // or to track and clear all granted updaters at removal time.
    emit ProviderRemoved(provider);
}
```

### Proof of Concept
1. Admin calls `createPriceProvider(...)` → `provider` deployed, `providerOwner[provider] = admin`, `isUpdater[provider][alice] = false`.
2. Admin calls `grantUpdater(provider, alice)` → `isUpdater[provider][alice] = true`.
3. Admin calls `removeProvider(provider)` → provider removed from `_providers`; `isUpdater[provider][alice]` remains `true`.
4. Admin calls `addProvider(provider)` → provider re-added to `_providers`; `isUpdater[provider][alice]` still `true`.
5. Alice calls `setConfidence([provider], [0])`:
   - `_providers.contains(provider)` → `true` ✓
   - `_requireUpdater(provider)` → `isUpdater[provider][alice]` is `true` ✓
   - `PriceProvider(provider).setConfidenceParam(0)` executes → `confidenceParam = 0`.
6. Next pool swap calls `getBidAndAskPrice()` → `adjustedSpread = spread * 0 = 0` → with `marginStep = 0`, `bidOut == askOut` → `FeedStalled` revert → swap DoS. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L34-37)
```text
    function _requireUpdater(address provider) internal view {
        if (msg.sender != providerOwner[provider] && !isUpdater[provider][msg.sender])
            revert NotProviderUpdater();
    }
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L106-116)
```text
    function addProvider(address provider) external override onlyRole(ADMIN_ROLE) {
        require(PriceProvider(provider).factory() == address(this));
        if (!_providers.add(provider)) revert ProviderAlreadyTracked();

        address owner = providerOwner[provider];
        if (owner != address(0)) {
            _providersByCreator[owner].add(provider);
        }

        emit ProviderAdded(provider);
    }
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L118-127)
```text
    function removeProvider(address provider) external override onlyRole(ADMIN_ROLE) {
        if (!_providers.remove(provider)) revert ProviderNotTracked();

        address owner = providerOwner[provider];
        if (owner != address(0)) {
            _providersByCreator[owner].remove(provider);
        }

        emit ProviderRemoved(provider);
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L216-228)
```text
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);

        // 6. Apply marginStep adjustment
        (uint256 bidOut, bool bidOk) = _applyBidAdjustments(bid);
        if (!bidOk || bidOut > type(uint128).max) return (0, type(uint128).max);

        (uint256 askOut, bool askOk) = _applyAskAdjustments(ask);
        if (!askOk || askOut > type(uint128).max) return (0, type(uint128).max);

        // 7. Hard invariant: bid must be strictly less than ask.
        //    Can be violated when marginStep < 0 and confidence is too small.
        if (bidOut >= askOut) return (0, type(uint128).max);
```
