### Title
Stale `isUpdater` Permissions After Provider Ownership Transfer Allow Unauthorized `confidenceParam` Zeroing, Collapsing Oracle Spread to Zero — (`PriceProviderFactory.sol` / `AnchoredProviderFactory.sol`)

---

### Summary

`transferProviderOwnership` in both `PriceProviderFactory` and `AnchoredProviderFactory` updates `providerOwner` and `_providersByCreator` but **never clears `isUpdater[provider][*]`**. Every address the previous owner granted updater rights to retains the ability to call `setConfidence` on the transferred provider indefinitely. For a `PriceProvider` with `marginStep = 0`, an old updater can set `confidenceParam = 0`, collapsing the oracle-derived spread to zero and causing every swap to execute at essentially mid price — eliminating the spread fee income that LPs depend on.

---

### Finding Description

**Root cause — stale mapping in `transferProviderOwnership`:**

In `PriceProviderFactory`: [1](#0-0) 

The function updates `providerOwner` and `_providersByCreator` but leaves `isUpdater[provider][oldUpdater]` untouched. The same pattern exists in `AnchoredProviderFactory`: [2](#0-1) 

**Stale permission survives the transfer:**

`setConfidence` calls `_requireUpdater`, which passes for any address where `isUpdater[provider][addr] == true` — regardless of whether that address was granted by the *current* or a *previous* owner: [3](#0-2) [4](#0-3) 

**Corrupted price path — `confidenceParam = 0` collapses the spread:**

In `PriceProvider._getBidAndAskPrice()`, the oracle spread is multiplied by `confidenceParam`: [5](#0-4) 

When `confidenceParam = 0`: `adjustedSpread = 0` → `delta = 0` → `bid = mid`, `ask = mid`. With `marginStep = 0`, both `stepBidFactor` and `stepAskFactor` equal `BPS_BASE_U`, so `bidOut ≈ askOut` (differing only by Floor vs Ceil rounding — at most 1 unit). The oracle's actual uncertainty is completely discarded. [6](#0-5) 

**Corrupted bid/ask reaches the pool swap:**

The pool calls `getBidAndAskPrice()` and derives `baseFeeX64` from the bid/ask spread: [7](#0-6) [8](#0-7) 

With a near-zero spread, `baseFeeX64 ≈ 0`. Every swap executes at essentially mid price — the spread fee that compensates LPs for inventory risk is eliminated.

---

### Impact Explanation

- **Bad-price execution**: Swaps execute at mid price with no oracle-spread component. The pool's `spreadFeeE6` (a fixed admin/protocol fee) still applies, but the variable `baseFeeX64` — which is the primary compensation for the oracle-anchored spread — is zeroed. LPs providing liquidity against a live oracle receive no spread income for the duration of the attack.
- **Scope**: Any pool whose mutable `PriceProvider` (with `marginStep = 0`) has had its ownership transferred is vulnerable for as long as the stale updater mapping persists.
- **New owner's remediation gap**: The

### Citations

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L34-37)
```text
    function _requireUpdater(address provider) internal view {
        if (msg.sender != providerOwner[provider] && !isUpdater[provider][msg.sender])
            revert NotProviderUpdater();
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

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L137-141)
```text
    function _getBidAskFrom(uint256 midPrice, uint256 confidence) internal pure returns (uint256 bid, uint256 ask) {
        uint256 delta = midPrice * confidence / CONFIDENCE_BASE;
        bid = delta >= midPrice ? 0 : midPrice - delta;
        ask = midPrice + delta;
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L215-218)
```text
        //    confidenceParam multiplies oracle spread; 0 means no spread
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(mid, adjustedSpread);

```

**File:** metric-core/contracts/MetricOmmPool.sol (L242-245)
```text
    (uint256 midPriceX64, uint256 baseFeeX64) =
      SwapMath.midAndSpreadFeeX64FromBidAsk(uint256(bidPriceX64), uint256(askPriceX64));
    SwapMath.InternalSwapParams memory params =
      SwapMath.InternalSwapParams({midPriceX64: midPriceX64, baseFeeX64: baseFeeX64, priceLimitX64: priceLimitX64});
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
