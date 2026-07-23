### Title
Stale `isUpdater` grants persist through `transferProviderOwnership`, allowing a previous owner's updater to manipulate `confidenceParam` and eliminate LP spread — (`smart-contracts-poc/contracts/PriceProviderFactory.sol`, `PriceProviderFactoryL2.sol`, `AnchoredProviderFactory.sol`)

---

### Summary

`transferProviderOwnership` updates `providerOwner` and the creator-set bookkeeping but never clears the `isUpdater[provider][*]` mapping. Every updater address granted by the previous owner retains the ability to call `setConfidence` on the provider after the transfer. The new owner has no way to enumerate those stale grants and cannot revoke them without knowing the exact addresses. A stale updater can set `confidenceParam = 0` on a `PriceProvider`, collapsing the bid/ask spread to near-zero and causing the pool to execute swaps at mid price, stripping LPs of their owed spread income.

---

### Finding Description

All three factory contracts share the same `transferProviderOwnership` implementation pattern:

```solidity
// PriceProviderFactory.sol lines 92-102
function transferProviderOwnership(address provider, address newOwner)
    external override onlyProviderOwner(provider)
{
    require(_providers.contains(provider), ProviderNotTracked());
    require(newOwner != address(0));
    address previousOwner = providerOwner[provider];

    providerOwner[provider] = newOwner;
    _providersByCreator[previousOwner].remove(provider);
    _providersByCreator[newOwner].add(provider);

    emit ProviderOwnershipTransferred(provider, previousOwner, newOwner);
}
``` [1](#0-0) 

The `isUpdater` mapping is a two-dimensional mapping keyed on `(provider, updater)`:

```solidity
mapping(address provider => mapping(address updater => bool)) public isUpdater;
``` [2](#0-1) 

`grantUpdater` sets `isUpdater[provider][updater] = true`; `revokeUpdater` sets it to `false`. Neither is called during `transferProviderOwnership`. After the transfer, `_requireUpdater` still passes for any address the previous owner had granted:

```solidity
function _requireUpdater(address provider) internal view {
    if (msg.sender != providerOwner[provider] && !isUpdater[provider][msg.sender])
        revert NotProviderUpdater();
}
``` [3](#0-2) 

There is no enumeration function for `isUpdater` entries. The new owner cannot discover which addresses were granted by the previous owner and therefore cannot revoke them.

The stale updater calls `setConfidence([provider], [0])`, which passes `_requireUpdater` and calls `PriceProvider.setConfidenceParam(0)`:

```solidity
confidenceParam = newValue;   // set to 0
lastConfidenceUpdate = block.timestamp;
``` [4](#0-3) 

Inside `_getBidAndAskPrice`, `confidenceParam = 0` collapses the spread:

```solidity
uint256 adjustedSpread = spread * confidenceParam;   // = 0
(uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);
// bid = mid, ask = mid
``` [5](#0-4) 

With `bid = ask = mid`, after applying `stepBidFactor`/`stepAskFactor` (both equal to `BPS_BASE_U` when `marginStep = 0`), `bidOut ≈ askOut`. The pool then executes swaps at mid price with effectively zero spread — the LP's compensation for providing liquidity is eliminated.

The identical pattern exists in `PriceProviderFactoryL2.transferProviderOwnership` and `AnchoredProviderFactory.transferProviderOwnership`. [6](#0-5) [7](#0-6) 

---

### Impact Explanation

A stale updater sets `confidenceParam = 0` on a `PriceProvider` used by a live pool. The pool's `getBidAndAskPrice()` returns `bid ≈ ask ≈ mid`, meaning every swap executes at mid price with no spread. LPs receive zero spread income for the duration (up to `CONFIDENCE_COOLDOWN = 1 minute` per manipulation window, repeatable indefinitely). This is a direct loss of owed LP assets — the spread is the LP's sole compensation in an oracle-anchored pool with no internal price discovery.

For `AnchoredPriceProvider`, the band clamp (`min(refBid, cBid)` / `max(refAsk, cAsk)`) restores the band edges when `confidenceParam = 0`, so the impact there is limited. The critical path is `PriceProvider` / `PriceProviderFactory` / `PriceProviderFactoryL2`. [8](#0-7) 

---

### Likelihood Explanation

- Ownership transfers are a normal operational event (e.g., protocol handoffs, DAO transitions).
- The previous owner may have granted updater access to multiple addresses (team members, bots) before the transfer.
- The previous owner could deliberately grant updater access to a controlled address immediately before transferring ownership, retaining covert influence over `confidenceParam` post-transfer.
- The new owner has no on-chain mechanism to discover or enumerate stale grants.
- The `CONFIDENCE_COOLDOWN` of 1 minute limits frequency but does not prevent the attack.

---

### Recommendation

Clear all updater grants for the provider during ownership transfer, or introduce an enumerable set of updaters per provider so the new owner can revoke them. The minimal fix mirrors the Hats Protocol recommendation — delete the stale state at transfer time:

```diff
function transferProviderOwnership(address provider, address newOwner)
    external override onlyProviderOwner(provider)
{
    require(_providers.contains(provider), ProviderNotTracked());
    require(newOwner != address(0));
    address previousOwner = providerOwner[provider];

    providerOwner[provider] = newOwner;
    _providersByCreator[previousOwner].remove(provider);
    _providersByCreator[newOwner].add(provider);

+   // Stale updater grants from the previous owner must not survive the transfer.
+   // Use an EnumerableSet<address> per provider to enumerate and clear them here,
+   // or emit an event listing all active updaters so the new owner can revoke them.

    emit ProviderOwnershipTransferred(provider, previousOwner, newOwner);
}
```

Apply the same fix to `PriceProviderFactoryL2` and `AnchoredProviderFactory`.

---

### Proof of Concept

1. Alice deploys a `PriceProvider` via `PriceProviderFactory.createPriceProvider(...)`. She is `providerOwner[provider]`.
2. Alice calls `grantUpdater(provider, eve)` — `isUpdater[provider][eve] = true`.
3. Alice calls `transferProviderOwnership(provider, bob)` — `providerOwner[provider] = bob`. `isUpdater[provider][eve]` is **not** cleared.
4. Bob is now the owner. He does not know Eve was granted updater access.
5. Eve calls `factory.setConfidence([provider], [0])`.
   - `_requireUpdater(provider)`: `msg.sender != bob` but `isUpdater[provider][eve] == true` → passes.
   - `provider.setConfidenceParam(0)` executes: `confidenceParam = 0`.
6. Any pool using this provider now calls `getBidAndAskPrice()` and receives `bid ≈ ask ≈ mid` (zero spread).
7. Swaps execute at mid price; LPs earn no spread for the next `CONFIDENCE_COOLDOWN` window.
8. Eve repeats every minute indefinitely. [9](#0-8) [10](#0-9)

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L20-20)
```text
    mapping(address provider => mapping(address updater => bool)) public isUpdater;
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L34-37)
```text
    function _requireUpdater(address provider) internal view {
        if (msg.sender != providerOwner[provider] && !isUpdater[provider][msg.sender])
            revert NotProviderUpdater();
    }
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L80-90)
```text
    function grantUpdater(address provider, address updater) external override onlyProviderOwner(provider) {
        require(_providers.contains(provider), ProviderNotTracked());
        isUpdater[provider][updater] = true;
        emit UpdaterGranted(provider, updater);
    }

    function revokeUpdater(address provider, address updater) external override onlyProviderOwner(provider) {
        require(_providers.contains(provider), ProviderNotTracked());
        isUpdater[provider][updater] = false;
        emit UpdaterRevoked(provider, updater);
    }
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L92-102)
```text
    function transferProviderOwnership(address provider, address newOwner) external override onlyProviderOwner(provider) {
        require(_providers.contains(provider), ProviderNotTracked());
        require(newOwner != address(0));
        address previousOwner = providerOwner[provider];

        providerOwner[provider] = newOwner;
        _providersByCreator[previousOwner].remove(provider);
        _providersByCreator[newOwner].add(provider);

        emit ProviderOwnershipTransferred(provider, previousOwner, newOwner);
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L92-103)
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
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L191-217)
```text
    function _getBidAndAskPrice() internal returns (uint128, uint128) {
        // 1. Read via the unified price(feedId, pool) path, forwarding the pool (msg.sender).
        //    refTime is already in seconds.
        (uint256 mid, uint256 spread, , uint256 refTime) =
            IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);

        // 2. Staleness check
        if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA)) {
            return (0, type(uint128).max);
        }

        // 3. Basic validity — price must be positive, spread must not be stalled marker
        if (mid == 0 || spread >= ORACLE_BPS) {
            return (0, type(uint128).max);
        }

        // 4. Price guard check (moved from oracle)
        (uint128 guardMin, uint128 guardMax) = offchainOracle.priceGuard(offchainFeedId);
        guardMax = guardMax == 0 ? type(uint128).max : guardMax;
        if (mid < guardMin || mid > guardMax) {
            return (0, type(uint128).max);
        }

        // 5. Compute bid/ask from mid + confidence-adjusted spread
        //    confidenceParam multiplies oracle spread; 0 means no spread
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);
```

**File:** smart-contracts-poc/contracts/PriceProviderFactoryL2.sol (L95-105)
```text
    function transferProviderOwnership(address provider, address newOwner) external override onlyProviderOwner(provider) {
        require(_providers.contains(provider), ProviderNotTracked());
        require(newOwner != address(0));
        address previousOwner = providerOwner[provider];

        providerOwner[provider] = newOwner;
        _providersByCreator[previousOwner].remove(provider);
        _providersByCreator[newOwner].add(provider);

        emit ProviderOwnershipTransferred(provider, previousOwner, newOwner);
    }
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L230-240)
```text
    function transferProviderOwnership(address provider, address newOwner) external override onlyProviderOwner(provider) {
        require(_providers.contains(provider), ProviderNotTracked());
        require(newOwner != address(0));
        address previousOwner = providerOwner[provider];

        providerOwner[provider] = newOwner;
        _providersByCreator[previousOwner].remove(provider);
        _providersByCreator[newOwner].add(provider);

        emit ProviderOwnershipTransferred(provider, previousOwner, newOwner);
    }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L342-346)
```text
        uint256 bidOut = Math.min(refBid, cBid);
        uint256 askOut = Math.max(refAsk, cAsk);
        if (bidOut == 0 || bidOut >= askOut) {
            return (0, type(uint128).max);
        }
```
