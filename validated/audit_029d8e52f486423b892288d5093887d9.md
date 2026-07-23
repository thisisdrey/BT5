### Title
Stale Updater Permissions Persist After `transferProviderOwnership`, Allowing Ex-Owner's Updaters to Manipulate `confidenceParam` Bid/Ask Spread — (`smart-contracts-poc/contracts/PriceProviderFactory.sol`, `AnchoredProviderFactory.sol`)

---

### Summary

`transferProviderOwnership` in both `PriceProviderFactory` and `AnchoredProviderFactory` updates `providerOwner[provider]` to the new owner but never clears the `isUpdater[provider][updater]` mappings that the previous owner had granted. Any address the previous owner authorized as an updater retains the ability to call `setConfidence` on the provider indefinitely after the transfer. Because `isUpdater` is a non-enumerable nested mapping, the new owner has no way to discover which addresses to revoke.

---

### Finding Description

`transferProviderOwnership` in `PriceProviderFactory`:

```solidity
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

The identical pattern exists in `AnchoredProviderFactory`: [2](#0-1) 

Neither function touches `isUpdater[provider][*]`. The `isUpdater` storage is:

```solidity
mapping(address provider => mapping(address updater => bool)) public isUpdater;
``` [3](#0-2) 

Because this is a nested mapping (not an `EnumerableSet`), the new owner cannot iterate over previously-granted updater addresses. The only remediation path is `revokeUpdater`, which requires knowing each address: [4](#0-3) 

A stale updater can call `setConfidence` through the factory:

```solidity
function setConfidence(
    address[] calldata providers,
    uint256[] calldata values
) external override {
    ...
    _requireUpdater(providers[i]);
    PriceProvider(providers[i]).setConfidenceParam(values[i]);
}
``` [5](#0-4) 

`_requireUpdater` passes for any address where `isUpdater[provider][msg.sender] == true`, regardless of whether the granting owner still owns the provider: [6](#0-5) 

---

### Impact Explanation

In `PriceProvider._getBidAndAskPrice()`, `confidenceParam` directly scales the oracle spread to compute the bid/ask delta:

```solidity
uint256 adjustedSpread = spread * confidenceParam;
(uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);
``` [7](#0-6) 

`_getBidAskFrom` computes:

```solidity
uint256 delta = midPrice * confidence / CONFIDENCE_BASE;
bid = delta >= midPrice ? 0 : midPrice - delta;
ask = midPrice + delta;
``` [8](#0-7) 

**Attack path — `confidenceParam` forced to 0 with `marginStep > 0`:**

- `adjustedSpread = 0` → `delta = 0` → `bid = mid`, `ask = mid`
- After `_applyBidAdjustments` / `_applyAskAdjustments` with `marginStep > 0`:
  - `bidOut = mid × stepBidFactor / BPS_BASE_U` (< mid in Q64)
  - `askOut = mid × stepAskFactor / BPS_BASE_U` (> mid in Q64)
- The pool now quotes a spread derived **only from `marginStep`**, with zero oracle-confidence component
- This is strictly tighter than the spread the new owner intended (which included the oracle's uncertainty)
- Traders can swap at near-mid prices, extracting value from LPs who bear the full impermanent-loss risk without the compensating spread

**Attack path — `confidenceParam` forced to 0 with `marginStep = 0`:**

- `bidOut == askOut` → `bidOut >= askOut` guard fires → returns `(0, type(uint128).max)` → `getBidAndAskPrice` reverts `FeedStalled`
- Pool swaps are completely blocked (DoS) [9](#0-8) 

`PriceProvider` has **no band clamp** (unlike `AnchoredPriceProvider`), so the corrupted quote reaches the pool unguarded. `AnchoredPriceProvider` with `MUTABLE_PARAMS = true` is also affected but the band clamp limits the damage to spread widening only. [10](#0-9) 

---

### Likelihood Explanation

- Provider ownership transfer is an explicitly supported, permissionless operation available to any provider owner.
- Granting updaters before a sale/transfer is a normal operational pattern (e.g., a market-maker bot as updater).
- The previous owner need not be malicious: a compromised updater key, or a seller who simply forgets to revoke, produces the same outcome.
- The `CONFIDENCE_COOLDOWN` of 1 minute does not prevent the attack — it only rate-limits updates; once `confidenceParam` is set to 0 it stays there until the new owner discovers and revokes the updater.
- The new owner has no on-chain mechanism to enumerate stale updaters. [11](#0-10) 

---

### Recommendation

Clear all updater grants atomically inside `transferProviderOwnership`. Because the current `isUpdater` mapping is not enumerable, the fix requires one of:

1. **Replace `isUpdater` with an `EnumerableSet`** per provider, and iterate to delete all entries on transfer.
2. **Add a per-provider updater-generation counter** (`updaterGeneration[provider]`): increment it on transfer; `_requireUpdater` checks both the flag and the generation at grant time. Stale grants from a prior generation are silently invalid.
3. **Require the caller to supply the list of updaters to revoke** as part of `transferProviderOwnership`, reverting if any granted updater is omitted.

---

### Proof of Concept

```
Setup:
  Alice deploys PriceProvider via PriceProviderFactory (marginStep = 5e16, i.e. 5%)
  Alice grants Bob as updater: factory.grantUpdater(provider, Bob)
  Alice sets confidenceParam = 500_000 (50x): factory.setConfidence([provider], [500_000])
  Pool now quotes bid ≈ mid×0.50, ask ≈ mid×1.50 (wide, oracle-confidence-driven)

Transfer:
  Alice calls factory.transferProviderOwnership(provider, Carol)
  isUpdater[provider][Bob] is still true

Attack (Bob, 1 minute after last update):
  Bob calls factory.setConfidence([provider], [0])
  → PriceProvider.setConfidenceParam(0) succeeds (Bob still passes _requireUpdater)
  → confidenceParam = 0

Effect on next swap:
  adjustedSpread = spread * 0 = 0
  bid = mid, ask = mid
  bidOut = mid × (BPS_BASE_U − marginStep) / BPS_BASE_U  (= mid × 0.95 in Q64)
  askOut = mid × (BPS_BASE_U + marginStep) / BPS_BASE_U  (= mid × 1.05 in Q64)
  Pool quotes 5% spread instead of oracle-confidence-driven spread
  Informed traders swap at near-mid, LPs absorb impermanent loss without compensating fee income

Carol cannot discover Bob's address from on-chain state alone (non-enumerable mapping).
```

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

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L86-90)
```text
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

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L230-239)
```text
    function transferProviderOwnership(address provider, address newOwner) external override onlyProviderOwner(provider) {
        require(_providers.contains(provider), ProviderNotTracked());
        require(newOwner != address(0));
        address previousOwner = providerOwner[provider];

        providerOwner[provider] = newOwner;
        _providersByCreator[previousOwner].remove(provider);
        _providersByCreator[newOwner].add(provider);

        emit ProviderOwnershipTransferred(provider, previousOwner, newOwner);
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L14-14)
```text
    uint256 public constant CONFIDENCE_COOLDOWN = 1 minutes;
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L137-141)
```text
    function _getBidAskFrom(uint256 midPrice, uint256 confidence) internal pure returns (uint256 bid, uint256 ask) {
        uint256 delta = midPrice * confidence / CONFIDENCE_BASE;
        bid = delta >= midPrice ? 0 : midPrice - delta;
        ask = midPrice + delta;
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L216-217)
```text
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L226-230)
```text
        // 7. Hard invariant: bid must be strictly less than ask.
        //    Can be violated when marginStep < 0 and confidence is too small.
        if (bidOut >= askOut) return (0, type(uint128).max);

        return (uint128(bidOut), uint128(askOut));
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
