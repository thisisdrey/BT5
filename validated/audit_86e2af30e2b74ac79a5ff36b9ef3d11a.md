### Title
Mutable `confidenceParam` Allows Provider Owner to Silently Widen Bid/Ask Spread After Pool Deployment, Causing Traders to Receive Worse Prices Than Quoted — (`smart-contracts-poc/contracts/PriceProvider.sol`, `ProtectedPriceProvider.sol`)

### Summary

The `confidenceParam` storage variable in `PriceProvider`, `PriceProviderL2`, `ProtectedPriceProvider`, and `ProtectedPriceProviderL2` is mutable after the provider is deployed and bound to a live pool. The provider owner (a permissionlessly created, semi-trusted role) or any address they grant as an updater can call `setConfidence()` on the factory at any time (subject only to a 1-minute cooldown), instantly changing the bid/ask spread used in all subsequent swaps. Traders who obtain a price quote via `getSellAndBuyPrices()` and then submit a swap can receive a materially worse execution price if `confidenceParam` is raised between the quote and the swap.

### Finding Description

`PriceProvider._getBidAndAskPrice()` computes the bid/ask spread as:

```solidity
uint256 adjustedSpread = spread * confidenceParam;
(uint256 bid, uint256 ask) = _getBidAskFrom(price, adjustedSpread);
``` [1](#0-0) 

where `confidenceParam` is a mutable storage slot initialized to zero and updatable by the factory:

```solidity
function setConfidenceParam(uint256 newValue) external {
    require(msg.sender == factory, OnlyFactory());
    if (newValue > CONFIDENCE_MAX) revert ConfidenceParamOutOfBounds();
    if (block.timestamp < lastConfidenceUpdate + CONFIDENCE_COOLDOWN) revert CooldownNotElapsed();
    confidenceParam = newValue;
    ...
}
``` [2](#0-1) 

`CONFIDENCE_MAX = 1_000_000` (100× multiplier) and `CONFIDENCE_COOLDOWN = 1 minutes`. [3](#0-2) 

The factory's `setConfidence()` is callable by the provider owner **or any granted updater** — roles that are created permissionlessly (anyone can call `createPriceProvider`): [4](#0-3) 

The provider owner is simply whoever called `createPriceProvider`, not a privileged protocol admin: [5](#0-4) 

Unlike `AnchoredPriceProvider`, which clamps the shaped quote to a reference band (`bidOut = Math.min(refBid, cBid)`), `PriceProvider` and `ProtectedPriceProvider` have **no such clamp**. The `confidenceParam` directly and unboundedly widens the spread delivered to the pool's swap math. [6](#0-5) 

The pool calls `getBidAndAskPrice()` at swap time, consuming whatever `confidenceParam` is current: [7](#0-6) 

### Impact Explanation

With oracle `spread = 500` (5 bps) and `confidenceParam` raised from 0 to `CONFIDENCE_MAX = 1_000_000`:

```
adjustedSpread = 500 × 1_000_000 = 500_000_000
delta          = mid × 500_000_000 / 1e10 = mid × 5%
bid            = mid × 0.95
ask            = mid × 1.05
```

The effective spread jumps from ~0 bps (only `marginStep`) to ~1000 bps (10%). A trader who quoted a tight spread and submitted a swap with a matching slippage tolerance receives ~5% fewer tokens than the pre-swap quote indicated. On a $1 M swap this is a $50,000 shortfall. The difference accrues to LPs, not to the provider owner, but the trader suffers a direct, unannounced loss of principal.

### Likelihood Explanation

The trigger is the provider owner or any granted updater — a semi-trusted role created permissionlessly. The only gate is a 1-minute cooldown, which is trivially short. The attack requires no special privilege beyond owning or being granted updater rights on the provider, and no on-chain signal warns pending swap transactions that `confidenceParam` changed. The scenario is directly analogous to the SecondSwap M-07 pattern: a mutable fee/spread parameter is changed after the pool is live, and users have no protection at execution time.

### Recommendation

1. **Snapshot `confidenceParam` at pool creation** (or at the time a swap is initiated) and use the snapshotted value for the duration of the swap, analogous to how SecondSwap's fix snapshots `buyerFee`/`sellerFee` at listing time.
2. **Enforce a meaningful timelock** (e.g., 24–48 hours) on `confidenceParam` changes so that pending transactions can observe and react to the change before it takes effect.
3. **Expose the pending `confidenceParam` change** via an event and a view function so off-chain tooling and traders can detect it before submitting swaps.
4. **Add a per-swap slippage guard** that compares the actual bid/ask at execution against the quoted bid/ask and reverts if the deviation exceeds a user-supplied tolerance.

### Proof of Concept

1. Provider owner deploys `PriceProvider` with `confidenceParam = 0` (tight spread). Pool is created and traders begin using it.
2. Trader calls `pool.getSellAndBuyPrices()` → receives `bid = mid × stepBidFactor`, `ask = mid × stepAskFactor` (tight, near-mid quotes).
3. Trader submits a swap transaction with slippage tolerance matching the quoted spread.
4. Provider owner calls `factory.setConfidence([provider], [1_000_000])` in the same or a subsequent block (cooldown elapsed).
5. Trader's swap executes: `PriceProvider._getBidAndAskPrice()` now computes `adjustedSpread = oracleSpread × 1_000_000`, producing `bid ≈ mid × 0.95`, `ask ≈ mid × 1.05`.
6. The pool's `SwapMath.computeSwapStep` uses the new wide spread. The trader receives ~5% fewer tokens than quoted. The shortfall accrues to LPs. No revert occurs because the execution price is still within the trader's (now-stale) slippage tolerance.

### Citations

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

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L194-217)
```text
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

**File:** smart-contracts-poc/contracts/ProtectedPriceProvider.sol (L19-25)
```text
    uint256 public constant CONFIDENCE_COOLDOWN = 1 minutes;

    uint256 internal constant ORACLE_DECIMALS = 1e8;
    uint256 public  constant BPS_BASE_U = 1e18;
    int256  public  constant BPS_BASE   = int256(BPS_BASE_U);
    uint256 public  constant CONFIDENCE_MAX  = 1_000_000; // 100x multiplier
    uint256 internal constant CONFIDENCE_BASE = 1e10;     // 1e6 (0.01 bps) × 10_000 (multiplier base)
```

**File:** smart-contracts-poc/contracts/ProtectedPriceProvider.sol (L208-223)
```text
        // 4. Compute bid/ask from mid + confidence-adjusted spread
        uint256 adjustedSpread = spread * confidenceParam;
        (uint256 bid, uint256 ask) = _getBidAskFrom(price, adjustedSpread);

        // 5. Apply marginStep adjustment
        (uint256 bidOut, bool bidOk) = _applyBidAdjustments(bid);
        if (!bidOk || bidOut > type(uint128).max) return (0, type(uint128).max);

        (uint256 askOut, bool askOk) = _applyAskAdjustments(ask);
        if (!askOk || askOut > type(uint128).max) return (0, type(uint128).max);

        // 6. Hard invariant: bid must be strictly less than ask.
        if (bidOut >= askOut) return (0, type(uint128).max);

        return (uint128(bidOut), uint128(askOut));
    }
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

**File:** metric-core/contracts/MetricOmmPool.sol (L523-549)
```text
  function getSellAndBuyPrices()
    external
    nonReentrant(PoolActions.SWAP)
    returns (uint128 sellPriceX64, uint128 buyPriceX64)
  {
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
  }
```
