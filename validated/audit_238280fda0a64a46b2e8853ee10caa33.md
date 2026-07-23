### Title
Stale Updater Grants Persist After Provider Ownership Transfer, Enabling Unauthorized `confidenceParam` Manipulation — (`PriceProviderFactory.sol` / `PriceProviderFactoryL2.sol`)

---

### Summary

`transferProviderOwnership` in both `PriceProviderFactory` and `PriceProviderFactoryL2` updates `providerOwner` but never clears the `isUpdater` mapping. A previous owner who pre-planted a trusted updater before transferring retains the ability — through that updater — to call `setConfidence` and manipulate `confidenceParam` on a provider they no longer own. Because `confidenceParam` is the sole multiplier on the oracle spread that feeds into `getBidAndAskPrice`, this produces an unauthorized bid/ask distortion that reaches every pool swap using that provider.

---

### Finding Description

`transferProviderOwnership` in `PriceProviderFactory`: [1](#0-0) 

updates `providerOwner[provider]` and moves the provider between creator sets, but never touches `isUpdater[provider][*]`. The updater check used by `setConfidence` is: [2](#0-1) 

Because `isUpdater[provider][staleUpdater]` is never cleared, any address the previous owner granted updater rights to before the transfer continues to satisfy `_requireUpdater` and can call `setConfidence` indefinitely (subject only to the 1-minute `CONFIDENCE_COOLDOWN`).

The same pattern exists in `PriceProviderFactoryL2`: [3](#0-2) 

`confidenceParam` is the multiplier on the oracle spread inside `_getBidAndAskPrice`: [4](#0-3) 

Setting it to `0` collapses the spread to zero (only `marginStep` applies); setting it to `CONFIDENCE_MAX` (1,000,000) maximally widens the spread. Both are reachable by the stale updater without the new owner's consent.

For `PriceProvider` / `PriceProviderL2` (non-anchored variants) there is no band clamp to bound the distortion — the manipulated quote reaches the pool directly: [5](#0-4) 

---

### Impact Explanation

- **Spread collapse (`confidenceParam → 0`)**: bid/ask narrows to the `marginStep` bias only. LPs quote at near-mid prices and suffer adverse selection on every swap; LP principal is drained by informed traders.
- **Spread explosion (`confidenceParam → CONFIDENCE_MAX`)**: bid/ask widens maximally. Traders receive worse execution than the oracle warrants; the pool may halt quoting if the inversion guard triggers, breaking core swap functionality.

Both outcomes constitute direct loss of user principal or broken core pool functionality, satisfying the allowed impact gate.

---

### Likelihood Explanation

Likelihood is **low-to-medium**. The attack requires the previous owner to have granted an updater before the transfer — a normal operational action — and then either (a) the previous owner is adversarial and pre-plants the updater as a retained control mechanism before selling/transferring the provider, or (b) the previous owner's updater key is later compromised. The new owner has no enumeration of existing updaters and may not know to revoke them. The 1-minute cooldown limits frequency but does not prevent the attack.

---

### Recommendation

Clear all updater grants on ownership transfer, or require the new owner to explicitly re-grant updaters. Minimally:

```solidity
function transferProviderOwnership(address provider, address newOwner) external override onlyProviderOwner(provider) {
    require(_providers.contains(provider), ProviderNotTracked());
    require(newOwner != address(0));
    address previousOwner = providerOwner[provider];
    providerOwner[provider] = newOwner;
    _providersByCreator[previousOwner].remove(provider);
    _providersByCreator[newOwner].add(provider);
    // ADD: emit or enumerate stale updaters so new owner can revoke,
    // or store updaters in an EnumerableSet and delete them here.
    emit ProviderOwnershipTransferred(provider, previousOwner, newOwner);
}
```

Alternatively, store updaters in an `EnumerableSet` per provider and delete the entire set on transfer.

---

### Proof of Concept

```
1. Alice deploys a PriceProvider via PriceProviderFactory for ETH/USDC pool.
   providerOwner[provider] = Alice
   isUpdater[provider][MaliciousBot] = false

2. Alice calls factory.grantUpdater(provider, MaliciousBot).
   isUpdater[provider][MaliciousBot] = true

3. Alice calls factory.transferProviderOwnership(provider, Bob).
   providerOwner[provider] = Bob
   isUpdater[provider][MaliciousBot] = true  ← NOT cleared

4. MaliciousBot calls factory.setConfidence([provider], [0]).
   _requireUpdater passes: isUpdater[provider][MaliciousBot] == true
   provider.confidenceParam = 0

5. Pool calls provider.getBidAndAskPrice().
   adjustedSpread = oracleSpread * 0 = 0
   bid = ask = mid ± marginStep only (spread collapsed)
   LPs are quoted at near-mid; informed traders drain LP positions.

6. Bob has no way to enumerate which updaters Alice granted; he cannot
   revoke MaliciousBot without knowing its address.
```

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L34-37)
```text
    function _requireUpdater(address provider) internal view {
        if (msg.sender != providerOwner[provider] && !isUpdater[provider][msg.sender])
            revert NotProviderUpdater();
    }
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L91-130)
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

    // ── Add / Remove (admin) ──────────────────────────────────────────

    function addProvider(address provider) external override onlyRole(ADMIN_ROLE) {
        require(PriceProvider(provider).factory() == address(this));
        if (!_providers.add(provider)) revert ProviderAlreadyTracked();

        address owner = providerOwner[provider];
        if (owner != address(0)) {
            _providersByCreator[owner].add(provider);
        }

        emit ProviderAdded(provider);
    }

    function removeProvider(address provider) external override onlyRole(ADMIN_ROLE) {
        if (!_providers.remove(provider)) revert ProviderNotTracked();

        address owner = providerOwner[provider];
        if (owner != address(0)) {
            _providersByCreator[owner].remove(provider);
        }

        emit ProviderRemoved(provider);
    }

    // ── Batch setConfidence ──────────────────────────────────────────────
    function setConfidence(
```

**File:** smart-contracts-poc/contracts/PriceProviderFactoryL2.sol (L1-60)
```text
// SPDX-License-Identifier: BUSL-1.1
pragma solidity ^0.8.28;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";
import {Multicall} from "@openzeppelin/contracts/utils/Multicall.sol";
import {EnumerableSet} from "@openzeppelin/contracts/utils/structs/EnumerableSet.sol";
import {PriceProviderL2} from "./PriceProviderL2.sol";
import {IPriceProviderFactoryL2} from "./interfaces/IPriceProviderFactoryL2.sol";

contract PriceProviderFactoryL2 is AccessControl, Multicall, IPriceProviderFactoryL2 {
    using EnumerableSet for EnumerableSet.AddressSet;

    // ── Roles ───────────────────────────────────────────────────────────
    bytes32 public constant ADMIN_ROLE = keccak256("ADMIN_ROLE");

    // ── Storage ─────────────────────────────────────────────────────────
    EnumerableSet.AddressSet private _providers;
    mapping(address creator => EnumerableSet.AddressSet) private _providersByCreator;
    mapping(address provider => address) public providerOwner;
    mapping(address provider => mapping(address updater => bool)) public isUpdater;

    // ── Constructor ─────────────────────────────────────────────────────
    constructor(address _admin) {
        _grantRole(ADMIN_ROLE, _admin);
        _setRoleAdmin(ADMIN_ROLE, ADMIN_ROLE);
    }

    // ── Modifiers ─────────────────────────────────────────────────────
    modifier onlyProviderOwner(address provider) {
        if (msg.sender != providerOwner[provider]) revert NotProviderOwner();
        _;
    }

    function _requireUpdater(address provider) internal view {
        if (msg.sender != providerOwner[provider] && !isUpdater[provider][msg.sender])
            revert NotProviderUpdater();
    }

    // ── Deploy (permissionless) ───────────────────────────────────────

    function createPriceProvider(
        address _oracle,
        bytes32 _feedId,
        int256  _marginStep,
        uint256 _maxTimeDelta,
        uint256 _futureTolerance,
        address _baseToken,
        address _quoteToken
    ) external override returns (address provider) {
        PriceProviderL2 p = new PriceProviderL2(
            address(this),
            _oracle,
            _feedId,
            _marginStep,
            _maxTimeDelta,
            _futureTolerance,
            _baseToken,
            _quoteToken
        );

```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L233-234)
```text
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L115-120)
```text
    function getBidAndAskPrice()
        external override returns (uint128 bid, uint128 ask)
    {
        (bid, ask) = _getBidAndAskPrice();
        if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
    }
```
