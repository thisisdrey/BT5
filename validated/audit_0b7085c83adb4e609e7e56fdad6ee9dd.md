### Title
Permissionless `providerOwner` can set a malicious `IAnchorSource` that widens the bid/ask spread to near-zero/max, causing direct loss to swappers — (`smart-contracts-poc/contracts/AnchoredPriceProvider.sol`)

### Summary

`AnchoredProviderFactory.setSource()` is callable by any `providerOwner`, a role acquired permissionlessly via `createAnchoredProvider()`. The clamp in `AnchoredPriceProvider._computeBidAsk()` uses `Math.min(refBid, cBid)` / `Math.max(refAsk, cAsk)`, which only prevents the source from *tightening* the spread — it places no upper bound on how wide the source can push it. A malicious source returning `bid = 1`, `ask = type(uint128).max − 1` passes every validity check and propagates directly into pool swap pricing, causing sellers to receive near-zero token1 and buyers to pay an astronomical amount.

### Finding Description

**Factory path — permissionless ownership:**

`createAnchoredProvider()` in `AnchoredProviderFactory` is fully permissionless. The caller becomes `providerOwner[provider]`. [1](#0-0) 

`setSource()` is gated only on `onlyProviderOwner`, so any permissionless creator can call it at any time with no timelock: [2](#0-1) 

**Source validation — insufficient upper-spread guard:**

`_readSource()` rejects `srcBid == 0`, `srcBid >= srcAsk`, and `srcAsk > type(uint128).max`. A source returning `srcBid = 1`, `srcAsk = type(uint128).max − 1` passes all three checks: [3](#0-2) 

**Clamp direction — widens, does not cap:**

The contract header documents the invariant as:

```
bid = min(mid − spreadBps − minMargin, custom_bid)
ask = max(mid + spreadBps + minMargin, custom_ask)
```

The implementation faithfully follows this, but the consequence is that the source can push `bidOut` arbitrarily close to zero and `askOut` arbitrarily close to `type(uint128).max`: [4](#0-3) 

The `MAX_SPREAD_BPS` circuit breaker fires only on the *reference* oracle's `spreadBps`, not on the final output width: [5](#0-4) 

**Pool consumption — prices flow directly into swap execution:**

The pool calls `_getBidAndAskPriceX64()` → provider `getBidAndAskPrice()` and uses the result to compute `midPriceX64` and `baseFeeX64`, which set the marginal price for every swap in that block: [6](#0-5) 

With `bid = 1` and `ask = type(uint128).max − 1`, `midPriceX64` collapses to `sqrt(1 · (2^128 − 2)) ≈ 2^64`, but the sell price (`bidAfterSpread`) is computed from `marginalPriceX64 · Q64 / (Q64 + sellFeeX64)` anchored on a bin that was positioned relative to the *original* mid — the realized sell price for token0 approaches zero and the buy price approaches `type(uint128).max`.

### Impact Explanation

Any user executing a `swap()` through a pool whose `AnchoredPriceProvider` has a malicious source active at that moment will:

- **Sell token0 → token1**: receive near-zero token1 (sell price ≈ 0 in Q64).
- **Buy token0 with token1**: pay a near-`type(uint128).max` amount of token1.

Both outcomes represent direct, irreversible loss of user principal. The `isProvider()` predicate on `AnchoredProviderFactory` is the machine-checkable eligibility gate for public pools; a provider that passes it is indistinguishable from a legitimate one until the source is swapped. [7](#0-6) 

### Likelihood Explanation

`createAnchoredProvider()` requires no permission and no capital beyond gas. The attacker:

1. Deploys a provider with legitimate parameters (passes envelope validation).
2. Operates in reference mode initially — quotes are correct, attracting LP deposits and swap volume.
3. Deploys a malicious `IAnchorSource` contract off-chain (no on-chain registration needed).
4. Calls `setSource(provider, maliciousSource)` — single transaction, no timelock, no delay.
5. All swaps in the same block execute at the extreme spread.

The `IAnchorSource` interface is minimal and requires no privileged access to deploy: [8](#0-7) 

### Recommendation

Two complementary fixes:

1. **Cap the source-widened spread**: after computing `bidOut`/`askOut`, enforce a maximum relative spread (e.g., `askOut / bidOut ≤ MAX_SOURCE_SPREAD_RATIO`). Any source output that would exceed the cap should fail closed (`return (0, type(uint128).max)`), not silently widen.

2. **Add a timelock to `setSource`**: since the justification for the no-timelock design ("the band bounds any source at all times") only holds for *tightening*, not *widening*, a timelock (e.g., 24–48 h) gives users and LPs time to exit before a malicious source activates. This mirrors the recommendation in the external report to add sanity checks against the value returned from the price feed.

### Proof of Concept

```solidity
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IAnchorSource} from "contracts/interfaces/IAnchorSource.sol";

/// @notice Malicious source: returns the widest possible valid spread.
contract MaliciousSource is IAnchorSource {
    function getBidAndAskPrice() external pure override returns (uint128 bid, uint128 ask) {
        bid = 1;                          // passes srcBid == 0 check
        ask = type(uint128).max - 1;      // passes srcAsk > type(uint128).max check
    }
}

// Attack steps (Foundry pseudocode):
// 1. attacker = makeAddr("attacker");
// 2. vm.prank(attacker);
//    address provider = factory.createAnchoredProvider(
//        oracle, baseFeedId, bytes32(0), minMargin, maxStaleness, maxSpreadBps,
//        false, 0, baseToken, quoteToken
//    );
// 3. Deploy pool using `provider` — operates correctly, attracts LPs.
// 4. MaliciousSource ms = new MaliciousSource();
// 5. vm.prank(attacker);
//    factory.setSource(provider, address(ms));
// 6. User calls pool.swap(...) — receives ~0 token1 for token0 sold.
//    Assertion: amount1Delta ≈ 0 despite non-zero amount0Delta.
```

The `_readSource` staticcall with `SOURCE_GAS_LIMIT = 500_000` forwards to `MaliciousSource.getBidAndAskPrice()`, which returns in a few hundred gas. `srcBid = 1 > 0`, `srcBid < srcAsk`, `srcAsk ≤ type(uint128).max` — all checks pass. The clamp sets `bidOut = Math.min(refBid, 1) = 1` and `askOut = Math.max(refAsk, type(uint128).max − 1) = type(uint128).max − 1`. The pool's swap math then executes at this extreme spread, draining the swapper. [9](#0-8)

### Citations

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L196-201)
```text
        provider = address(p);
        address creator = msg.sender;

        _providers.add(provider);
        _providersByCreator[creator].add(provider);
        providerOwner[provider] = creator;
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L222-228)
```text
    /// @notice Swap a provider's source (zero → reference mode). The curator's only knob — instant,
    ///         no timelock: any source is clamp-bounded by the provider at all times.
    function setSource(address provider, address newSource) external override onlyProviderOwner(provider) {
        require(_providers.contains(provider), ProviderNotTracked());
        AnchoredPriceProvider(provider).setSource(newSource);
        emit SourceSet(provider, newSource);
    }
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L279-283)
```text
    /// @notice The public-pool eligibility predicate: deployed by this factory ⇒ clamp-bounded quotes
    ///         with parameters that were inside the envelope at deploy time.
    function isProvider(address provider) external view returns (bool) {
        return _providers.contains(provider);
    }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L302-305)
```text
        // Circuit breaker: extreme (combined) uncertainty means the feed is clearly broken.
        if (spreadBps > MAX_SPREAD_BPS) {
            return (0, type(uint128).max);
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

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L385-411)
```text
    function _readSource(address _source)
        internal view returns (bool ok, uint256 srcBid, uint256 srcAsk)
    {
        bytes4 sel = IAnchorSource.getBidAndAskPrice.selector;
        bool success;
        uint256 retSize;
        uint256 b;
        uint256 a;
        assembly ("memory-safe") {
            // Scratch beyond the free-memory pointer; never updated, so this is memory-safe.
            let ptr := mload(0x40)
            mstore(ptr, sel) // 4-byte selector, left-aligned
            // Input is consumed before output is written, so in/out may share ptr. Output is capped
            // at 0x40 bytes: a larger returndata is NOT copied (only returndatasize() reports it).
            success := staticcall(SOURCE_GAS_LIMIT, _source, ptr, 0x04, ptr, 0x40)
            retSize := returndatasize()
            b := mload(ptr)
            a := mload(add(ptr, 0x20))
        }
        if (!success || retSize != 64) return (false, 0, 0);

        srcBid = b;
        srcAsk = a;
        if (srcBid == 0 || srcBid >= srcAsk || srcAsk > type(uint128).max) return (false, 0, 0);

        return (true, srcBid, srcAsk);
    }
```

**File:** metric-core/contracts/MetricOmmPool.sol (L528-548)
```text
    (uint128 bidFromOracleX64, uint128 askFromOracleX64) = _getBidAndAskPriceX64();
    (uint256 midPriceX64, uint256 baseFeeX64) =
      SwapMath.midAndSpreadFeeX64FromBidAsk(uint256(bidFromOracleX64), uint256(askFromOracleX64));

    BinState memory binState = _binStates[curBinIdx];
    uint256 lowerPriceX64 = distanceE6ToPriceX64(curBinDistFromProvidedPriceE6, midPriceX64);
    uint256 upperPriceX64 =
      distanceE6ToPriceX64(_addDistE6(curBinDistFromProvidedPriceE6, binState.lengthE6), midPriceX64);

    uint256 marginalPriceX64 =
      SwapMath.calculatePriceAtBinPosition(lowerPriceX64, upperPriceX64, curPosInBin, Math.Rounding.Floor);

    uint256 buyFeeX64 = baseFeeX64 + Math.mulDiv(binState.addFeeBuyE6, ONE_X64, 1e6);
    uint256 sellFeeX64 = baseFeeX64 + Math.mulDiv(binState.addFeeSellE6, ONE_X64, 1e6);

    uint256 askBeforeNotional = Math.mulDiv(marginalPriceX64, ONE_X64 + buyFeeX64, ONE_X64, Math.Rounding.Ceil);
    uint256 bidAfterSpread = Math.mulDiv(marginalPriceX64, ONE_X64, ONE_X64 + sellFeeX64, Math.Rounding.Floor);

    uint256 nf = notionalFeeE8;
    buyPriceX64 = Math.mulDiv(askBeforeNotional, 1e8, 1e8 - nf, Math.Rounding.Ceil).toUint128();
    sellPriceX64 = Math.mulDiv(bidAfterSpread, 1e8 - nf, 1e8, Math.Rounding.Floor).toUint128();
```

**File:** smart-contracts-poc/contracts/interfaces/IAnchorSource.sol (L1-12)
```text
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

/// @notice Custom quote source for an AnchoredPriceProvider (source mode). Any contract — open or
///         opaque, deployed by anyone — may implement it; the provider clamps its quotes into the
///         reference band, so source code is never reviewed.
/// @dev    Q64 quotes, same convention as IPriceProvider.getBidAndAskPrice — any view provider
///         qualifies as a source. The provider calls this via a gas-bounded staticcall and fails
///         closed on revert, out-of-gas, malformed returndata, zero bid or bid >= ask.
interface IAnchorSource {
    function getBidAndAskPrice() external view returns (uint128 bid, uint128 ask);
}
```
