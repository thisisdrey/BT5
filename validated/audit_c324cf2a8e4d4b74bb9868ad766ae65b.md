### Title
`PriceProvider.getBidAndAskPrice()` reverts with `FeedStalled` on round oracle prices when `confidenceParam` is at its default zero value — (`File: smart-contracts-poc/contracts/PriceProvider.sol`)

---

### Summary

`PriceProvider` initializes `confidenceParam` to `0` (Solidity default) and the factory never sets it during deployment. When `marginStep == 0` and the oracle mid-price is divisible by `390625` (which includes every round dollar price such as $1.00, $2.00, $100.00), the floor and ceil rounding of the step-adjustment produce `bidOut == askOut`. The `bidOut >= askOut` guard then returns the `(0, type(uint128).max)` sentinel, causing `getBidAndAskPrice()` to revert with `FeedStalled` and blocking every swap on the pool.

---

### Finding Description

`PriceProvider._getBidAndAskPrice()` computes the bid/ask as follows when `confidenceParam == 0`:

```
adjustedSpread = spread * 0 = 0
bid = mid,  ask = mid          // _getBidAskFrom with delta = 0
```

With `marginStep == 0`, `stepBidFactor = stepAskFactor = BPS_BASE_U = 1e18`, so:

```
bidOut = floor(mid × Q64 × BPS_BASE_U / STEP_DENOM)
       = floor(mid × 2^64 / 1e8)

askOut = ceil (mid × 2^64 / 1e8)
```

`bidOut == askOut` exactly when `mid × 2^64 mod 1e8 == 0`.

Because `gcd(2^64, 1e8) = 2^8 = 256`, the condition reduces to `mid mod 390625 == 0`. Every integer-dollar oracle price satisfies this: $1.00 → `mid = 100_000_000 = 256 × 390625`, $2.00 → `200_000_000 = 512 × 390625`, $100.00 → `10_000_000_000 = 25600 × 390625`.

When `bidOut == askOut` the guard at line 228 fires:

```solidity
if (bidOut >= askOut) return (0, type(uint128).max);
```

`getBidAndAskPrice()` then hits:

```solidity
if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
```

and reverts, making every swap on the pool revert for as long as `confidenceParam` remains 0.

The factory `createPriceProvider` never initialises `confidenceParam`; it stays at the Solidity default of `0` until the provider owner explicitly calls `setConfidence`. There is also a mandatory 1-minute cooldown on that call, so the window cannot be closed atomically at deployment.

The `AnchoredPriceProvider` avoids this because its `_shapedQuote` deliberately omits the `sBid >= sAsk` pre-clamp halt and relies on the outer band clamp (`min(refBid, cBid)` / `max(refAsk, cAsk)`) to restore ordering. `PriceProvider` has no such safety net.

---

### Impact Explanation

Every swap on a pool whose price provider is a `PriceProvider` with `marginStep == 0` and `confidenceParam == 0` reverts with `FeedStalled` whenever the oracle mid-price is a multiple of 390625 (8-decimal units). Because major asset prices routinely land on round dollar values, the pool is rendered completely unusable for swaps during those periods. Liquidity providers cannot earn fees; traders cannot execute. This is broken core pool functionality / unusable swap flow.

---

### Likelihood Explanation

- `marginStep == 0` is a valid and natural default choice for a pool creator who wants no step bias.
- `confidenceParam` is never set by the factory at deployment; the default is `0`.
- Round dollar oracle prices (multiples of 390625 in 8-decimal representation) are extremely common for major assets (ETH, BTC, stablecoins).
- No privileged action is required to reach the broken state; it is the out-of-the-box state for any `PriceProvider` deployed with `marginStep == 0`.

---

### Recommendation

1. **Require a non-zero initial `confidenceParam`** in `createPriceProvider`, or set it atomically during construction (bypassing the cooldown for the first call).
2. Alternatively, add a minimum-spread floor in `_getBidAndAskPrice` so that `bidOut` and `askOut` are always strictly separated by at least one unit regardless of `confidenceParam`.
3. Mirror the `AnchoredPriceProvider` pattern: omit the pre-clamp `bidOut >= askOut` halt and instead rely on a band clamp to guarantee ordering.

---

### Proof of Concept

1. Deploy `PriceProvider` via `PriceProviderFactory.createPriceProvider` with `_marginStep = 0`. `confidenceParam` is `0` by default; the factory never sets it.
2. Attach this provider to a pool.
3. Wait for (or observe) the oracle to report `mid = 100_000_000` ($1.00 in 8 decimals).
4. Call `pool.swap(...)`. The pool calls `provider.getBidAndAskPrice()`.
5. Inside `_getBidAndAskPrice`: `adjustedSpread = 0`, `bid = ask = 100_000_000`. `_applyBidAdjustments` returns `floor(100_000_000 × 2^64 / 1e8) = 1844674407370955264`. `_applyAskAdjustments` returns `ceil(100_000_000 × 2^64 / 1e8) = 1844674407370955264` (identical, since `100_000_000 × 2^64` is exactly divisible by `1e8`).
6. `bidOut >= askOut` → returns `(0, type(uint128).max)` → `revert FeedStalled()`.
7. Every swap reverts until `setConfidence` is called with a non-zero value. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)

### Citations

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L115-120)
```text
    function getBidAndAskPrice()
        external override returns (uint128 bid, uint128 ask)
    {
        (bid, ask) = _getBidAndAskPrice();
        if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L173-187)
```text
    function _applyStepAdjustment(
        uint256        price,
        uint256        stepFactor,
        Math.Rounding  rounding
    ) private pure returns (uint256 out, bool ok) {
        if (price == 0) return (0, false);

        uint256 numerator = Q64 * stepFactor;

        out = Math.mulDiv(price, numerator, STEP_DENOM, rounding);

        if (out == 0) return (0, false);

        return (out, true);
    }
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L214-228)
```text
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
```

**File:** smart-contracts-poc/contracts/PriceProviderFactory.sol (L41-76)
```text
    function createPriceProvider(
        address _oracle,
        bytes32 _feedId,
        int256  _marginStep,
        uint256 _maxTimeDelta,
        address _baseToken,
        address _quoteToken
    ) external override returns (address provider) {
        PriceProvider p = new PriceProvider(
            address(this),
            _oracle,
            _feedId,
            _marginStep,
            _maxTimeDelta,
            _baseToken,
            _quoteToken
        );

        provider = address(p);
        address creator = msg.sender;

        _providers.add(provider);
        _providersByCreator[creator].add(provider);
        providerOwner[provider] = creator;

        emit ProviderDeployed(
            provider,
            creator,
            _feedId,
            _oracle,
            p.baseToken(),
            p.quoteToken(),
            _marginStep,
            _maxTimeDelta
        );
    }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L351-372)
```text
    /// @dev Shaped reference quote (customizable variant, reference mode): delta = mid·spreadBps·confidence,
    ///      then the marginStep step factors via `_bandEdge` (byte-identical to PriceProvider's step math).
    ///
    ///      Deliberately NO `sBid >= sAsk` pre-clamp halt (unlike PriceProvider): with knobs at 0
    ///      (confidence 0, marginStep 0) and
    ///      a round mid, floor == ceil gives sBid == sAsk, and the band clamp restores ordering —
    ///      that is exactly the immutable-variant identity. The `sAsk > uint128.max` check MUST stay
    ///      pre-clamp (mirrors `_readSource`'s srcAsk guard): truncating later would let an
    ///      overflowing ask wrap to a sub-band quote.
    function _shapedQuote(uint256 mid, uint256 spreadBps)
        internal view returns (bool ok, uint256 sBid, uint256 sAsk)
    {
        uint256 delta = mid * (spreadBps * confidenceParam) / CONFIDENCE_BASE;
        uint256 bid8 = delta >= mid ? 0 : mid - delta;
        uint256 ask8 = mid + delta;

        sBid = _bandEdge(bid8, stepBidFactor, Math.Rounding.Floor);
        sAsk = _bandEdge(ask8, stepAskFactor, Math.Rounding.Ceil);
        if (sBid == 0 || sAsk > type(uint128).max) return (false, 0, 0);

        return (true, sBid, sAsk);
    }
```
