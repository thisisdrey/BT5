### Title
`transferProviderOwnership` Does Not Clear Stale Updater Grants, Enabling Unauthorized `confidenceParam` Manipulation That Reaches Pool Swaps - (File: `smart-contracts-poc/contracts/PriceProviderFactory.sol`)

---

### Summary

`transferProviderOwnership` in `PriceProviderFactory`, `PriceProviderFactoryL2`, and `AnchoredProviderFactory` updates `providerOwner` but never clears the `isUpdater` mapping for addresses the previous owner had authorized. Those stale updaters retain the ability to call `setConfidence`, which writes directly to `confidenceParam` inside `PriceProvider` / `ProtectedPriceProvider`. Because `confidenceParam` is the sole multiplier on the oracle spread before bid/ask are computed and forwarded to the pool, a stale updater can drive the effective spread to zero (or to `CONFIDENCE_MAX`), producing a bad-price execution or a `FeedStalled` revert on every subsequent swap.

---

### Finding Description

`transferProviderOwnership` in all three factory contracts performs only three state mutations:

```
providerOwner[provider] = newOwner;
_providersByCreator[previousOwner].remove(provider);
_providersByCreator[newOwner].add(provider);
``` [1](#0-0) 

The `isUpdater[provider][updater]` mapping — which is the only other authorization gate — is never touched. After the transfer, `_requireUpdater` still passes for every address the previous owner had granted:

```solidity
function _requireUpdater(address provider) internal view {
    if (msg.sender != providerOwner[provider] && !isUpdater[provider][msg.sender])
        revert NotProviderUpdater();
}
``` [2](#0-1) 

`setConfidence` calls `_requireUpdater` and then directly invokes `PriceProvider.setConfidenceParam`: [3](#0-2) 

Inside `PriceProvider._getBidAndAskPrice`, `confidenceParam` is the sole multiplier on the oracle spread before bid/ask are computed:

```solidity
uint256 adjustedSpread = spread * confidenceParam;
(uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);
``` [4](#0-3) 

There is no reference-band clamp in `PriceProvider` (unlike `AnchoredPriceProvider`). Whatever `confidenceParam` produces is the final bid/ask forwarded to the pool.

The new owner cannot enumerate stale updaters to revoke them: `isUpdater` is a plain nested mapping with no accompanying set, so there is no on-chain way to discover which addresses were previously granted. The new owner would have to reconstruct the grant history from `UpdaterGranted` events and manually call `revokeUpdater` for each one — a race condition the stale updater wins by acting first.

The same defect exists verbatim in `PriceProviderFactoryL2.transferProviderOwnership` and `AnchoredProviderFactory.transferProviderOwnership`. [5](#0-4) [6](#0-5) 

---

### Impact Explanation

**Scenario A — Spread collapse / LP fund extraction (Medium-High)**

If the previous owner had set `confidenceParam` to a large value (e.g. `100_000`) to provide a protective spread, a stale updater reduces it to `0` or `1`. With `confidenceParam = 0`:

- `adjustedSpread = spread * 0 = 0`
- `bid = mid`, `ask = mid`
- After `_applyBidAdjustments` / `_applyAskAdjustments` with `marginStep = 0`: `bidOut == askOut` → `bidOut >= askOut` guard fires → `(0, type(uint128).max)` → `FeedStalled` revert on every swap.

With `confidenceParam = 1` and a positive `marginStep`, the spread collapses to only `2 × marginStep` in Q64 terms. Traders can execute swaps at near-mid prices, extracting value from LPs who deposited expecting the wider spread the previous owner had configured. [7](#0-6) 

**Scenario B — Provider stall / unusable swap flow (Medium)**

A stale updater sets `confidenceParam = CONFIDENCE_MAX` (`1_000_000`). For any oracle spread ≥ 1 bps, `delta = mid * spread * 1_000_000 / CONFIDENCE_BASE` overflows `mid`, forcing `bid = 0`. The provider returns `(0, type(uint128).max)` and `getBidAndAskPrice` reverts with `FeedStalled` on every pool swap until the new owner discovers and revokes the stale updater. [8](#0-7) 

Both scenarios satisfy the allowed impact gate: Scenario A is bad-price execution reaching pool swaps with LP fund impact; Scenario B is broken core pool functionality causing unusable swap flows.

---

### Likelihood Explanation

The trigger requires: (1) the previous owner to have granted at least one updater before transferring ownership, and (2) that updater to act maliciously or be compromised. Provider ownership transfer is an explicitly supported operation, and granting updaters for off-chain confidence management is the documented operational model. The stale updater needs only to call `setConfidence` once per `CONFIDENCE_COOLDOWN` (1 minute) to maintain the manipulated state, and the new owner has no enumerable list of grants to revoke. Likelihood is **Medium**.

---

### Recommendation

Clear all updater grants atomically inside `transferProviderOwnership`. Because `isUpdater` is a plain mapping (not enumerable), an accompanying `EnumerableSet` of per-provider updaters must be maintained:

```solidity
// Add to storage:
mapping(address provider => EnumerableSet.AddressSet) private _updaters;

// In grantUpdater:
_updaters[provider].add(updater);
isUpdater[provider][updater] = true;

// In revokeUpdater:
_updaters[provider].remove(updater);
isUpdater[provider][updater] = false;

// In transferProviderOwnership — add before updating providerOwner:
EnumerableSet.AddressSet storage updaterSet = _updaters[provider];
uint256 len = updaterSet.length();
for (uint256 i = len; i > 0; ) {
    unchecked { --i; }
    address u = updaterSet.at(i);
    isUpdater[provider][u] = false;
    updaterSet.remove(u);
    emit UpdaterRevoked(provider, u);
}
```

This mirrors the mitigation recommended in the OwnableSmartWallet report and is the only way to guarantee that a new owner inherits a clean authorization state. Apply the same fix to `PriceProviderFactoryL2` and `AnchoredProviderFactory`.

---

### Proof of Concept

```
State before transfer:
  providerOwner[P]       = Alice
  isUpdater[P][Bob]      = true   // Alice granted Bob
  isUpdater[P][Carol]    = true   // Alice granted Carol
  confidenceParam (in P) = 100_000  // wide spread, LPs protected

Step 1: Alice calls transferProviderOwnership(P, Dave)
  → providerOwner[P] = Dave
  → isUpdater[P][Bob]   still true  ← BUG
  → isUpdater[P][Carol] still true  ← BUG

Step 2: Bob calls setConfidence([P], [0])
  → _requireUpdater passes (isUpdater[P][Bob] == true)
  → PriceProvider(P).setConfidenceParam(0) succeeds
  → confidenceParam = 0

Step 3: Any user calls pool.swap(...)
  → pool calls PriceProvider(P).getBidAndAskPrice()
  → adjustedSpread = oracleSpread * 0 = 0
  → bid = mid, ask = mid
  → (with marginStep == 0) bidOut >= askOut → returns (0, max)
  → getBidAndAskPrice reverts FeedStalled
  → swap reverts — pool is bricked until Dave discovers Bob and revokes him

Alternative Step 2': Bob calls setConfidence([P], [1])
  → confidenceParam = 1 (spread collapses to oracle spread only)
  → Traders execute swaps at near-mid prices, extracting LP value
``` [9](#0-8) [10](#0-9)

### Citations

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

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L130-142)
```text
    function setConfidence(
        address[] calldata providers,
        uint256[] calldata values
    ) external override {
        uint256 l = providers.length;
        if (l != values.length) revert LengthMismatch();

        for (uint256 i; i < l; ++i) {
            require(_providers.contains(providers[i]), ProviderNotTracked());
            _requireUpdater(providers[i]);
            PriceProvider(providers[i]).setConfidenceParam(values[i]);
        }
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L92-104)
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
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L191-231)
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

        // 6. Apply marginStep adjustment
        (uint256 bidOut, bool bidOk) = _applyBidAdjustments(bid);
        if (!bidOk || bidOut > type(uint128).max) return (0, type(uint128).max);

        (uint256 askOut, bool askOk) = _applyAskAdjustments(ask);
        if (!askOk || askOut > type(uint128).max) return (0, type(uint128).max);

        // 7. Hard invariant: bid must be strictly less than ask.
        //    Can be violated when marginStep < 0 and confidence is too small.
        if (bidOut >= askOut) return (0, type(uint128).max);

        return (uint128(bidOut), uint128(askOut));
    }
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
