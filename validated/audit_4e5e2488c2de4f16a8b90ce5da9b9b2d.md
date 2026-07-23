The critical guard to check is `MetricOmmPoolFactory._validatePriceProvider`, which is the only on-chain check between a provider and a pool's token pair. Let me trace the full path.

**`_validatePriceProvider` (lines 541–546):**

```solidity
function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
        revert PriceProviderTokenMismatch();
    }
}
```

This check only validates that `provider.token0()` and `provider.token1()` match the pool's tokens. It does **not** validate that `baseFeedId` inside the provider corresponds to those tokens.

`AnchoredPriceProvider.token0()` returns `baseToken` and `token1()` returns `quoteToken` — both set at construction from the caller-supplied `_baseToken`/`_quoteToken` parameters. These are entirely independent of `baseFeedId`.

**The attack path is fully permissionless given normal admin state:**

1. Admin adds ETH/USD `ChainlinkOracle` to the allow-list and configures an envelope — normal operations, no malice required.
2. Attacker calls `AnchoredProviderFactory.createAnchoredProvider(oracle=ETH_USD_oracle, baseFeedId=ETH_USD_feedId, ..., baseToken=WBTC, quoteToken=USDC)` — permissionless, passes all checks (oracle in allow-list, params within envelope).
3. Attacker calls `MetricOmmPoolFactory.createPool(token0=WBTC, token1=USDC, priceProvider=misconfiguredProvider)` — permissionless, `_validatePriceProvider` passes because `provider.token0()=WBTC` and `provider.token1()=USDC` match.
4. Pool is deployed. Every swap calls `provider.getBidAndAskPrice()`, which reads `oracle.price(ETH_USD_feedId, pool)` — returning ETH/USD prices for a WBTC/USDC pool.

---

### Title
`AnchoredProviderFactory` and `MetricOmmPoolFactory` lack feedId-to-token correspondence validation, allowing permissionless deployment of a pool that reads the wrong oracle feed — (`metric-core/contracts/MetricOmmPoolFactory.sol`, `smart-contracts-poc/contracts/AnchoredProviderFactory.sol`)

### Summary
`MetricOmmPoolFactory._validatePriceProvider` only checks that `provider.token0()/token1()` match the pool's token pair. It does not verify that the provider's `baseFeedId` corresponds to those tokens. Because `AnchoredProviderFactory.createAnchoredProvider` is permissionless and also performs no feedId-to-token validation, an unprivileged caller can deploy a provider with WBTC/USDC token labels but an ETH/USD `baseFeedId`, then create a WBTC/USDC pool using that provider. The pool will price every swap at ETH/USD rates.

### Finding Description
`AnchoredPriceProvider` stores `baseToken`/`quoteToken` and `baseFeedId` as independent immutables with no on-chain relationship enforced between them. [1](#0-0) 

`AnchoredProviderFactory.createAnchoredProvider` is explicitly permissionless and validates only that the oracle is allow-listed and that numeric parameters fall within the admin-configured envelope. It performs no check that `baseFeedId` is the correct feed for `baseToken/quoteToken`. [2](#0-1) 

`MetricOmmPoolFactory._validatePriceProvider` — the sole guard at pool creation — only compares `provider.token0()/token1()` to the pool's tokens. Since those values come from the constructor-supplied `_baseToken`/`_quoteToken` (not from the oracle feed), a provider with mismatched `baseFeedId` passes this check silently. [3](#0-2) 

`_validatePoolParameters` calls `_validatePriceProvider` and nothing else regarding oracle correctness. [4](#0-3) 

At swap time, `AnchoredPriceProvider._readLeg` calls `oracle.price(baseFeedId, pool)` — the immutable, misconfigured `baseFeedId` — and the returned price is used directly for swap settlement. [5](#0-4) 

### Impact Explanation
Every swap in the misconfigured WBTC/USDC pool executes at ETH/USD prices. Arbitrageurs can drain LP principal by buying WBTC at ETH/USD price (far below BTC/USD) or selling WBTC at ETH/USD price (far above BTC/USD), depending on the price differential. This is direct, unbounded LP principal loss — a Critical impact.

### Likelihood Explanation
Both `createAnchoredProvider` and `createPool` are permissionless. The only prerequisite is that the admin has added at least one oracle to the allow-list and configured an envelope — both are normal, expected operational steps. No admin error or malice is required. Any external caller can execute the full attack once the system is live.

### Recommendation
In `_validatePriceProvider` (or in `AnchoredProviderFactory.createAnchoredProvider`), verify that the oracle actually holds a non-stale, non-zero price for `baseFeedId` at the time of provider/pool creation, **and** that the provider's `baseFeedId` is registered against the expected token pair in the oracle's feed registry. Alternatively, require the `AnchoredProviderFactory` to record a `(feedId → baseToken, quoteToken)` mapping (admin-set alongside `setFeedClass`) and enforce it at `createAnchoredProvider` time, so the mismatch is caught before a provider is ever deployed.

### Proof of Concept
```solidity
// 1. Admin setup (normal operations)
anchoredFactory.addOracle(address(ethUsdOracle));
anchoredFactory.setEnvelope(DEFAULT_CLASS, validEnvelope);

// 2. Attacker: create misconfigured provider (permissionless)
address badProvider = anchoredFactory.createAnchoredProvider(
    address(ethUsdOracle),  // oracle with ETH/USD data
    ETH_USD_FEED_ID,        // ETH/USD feed
    bytes32(0),
    minMargin, maxStaleness, maxSpread,
    false, 0,
    address(WBTC),          // token labels say WBTC/USDC
    address(USDC)
);

// 3. Attacker: create WBTC/USDC pool with bad provider (permissionless)
// _validatePriceProvider passes: badProvider.token0()==WBTC, token1()==USDC
address pool = poolFactory.createPool(PoolParameters({
    token0: address(WBTC), token1: address(USDC),
    priceProvider: badProvider, ...
}));

// 4. Innocent LP adds liquidity; attacker swaps at ETH/USD price
// getBidAndAskPrice() returns ETH/USD quote (~$2500) not BTC/USD (~$60000)
// Attacker buys WBTC at ETH price, draining LP principal
(uint128 bid, uint128 ask) = IPriceProvider(badProvider).getBidAndAskPrice();
assertApproxEqRel(toUSD(bid), ETH_USD_PRICE, 1e16); // matches ETH, not BTC
```

### Citations

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L65-70)
```text
    IOffchainOracle public immutable offchainOracle;
    bytes32         public immutable baseFeedId;
    /// @notice Optional second feed for synthetic ratio quoting; zero = single-feed (no conversion).
    ///         Synthetic mid = price(baseFeedId) / price(quoteFeedId), e.g. BTC/USD ÷ ETH/USD = BTC/ETH.
    bytes32         public immutable quoteFeedId;
    /// @dev anchor factory (governs setSource), NOT the AMM pool factory passed at read.
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L258-272)
```text
    function _getBidAndAskPrice() internal returns (uint128, uint128) {
        (uint256 mid, uint256 spreadBps, , bool ok) = _readLeg(baseFeedId);
        if (!ok) return (0, type(uint128).max);

        bytes32 _quote = quoteFeedId;
        if (_quote != bytes32(0)) {
            (uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLeg(_quote);
            if (!ok2 || mid2 == 0) return (0, type(uint128).max);
            // Synthetic ratio (8-decimal): mid1 / mid2. Relative uncertainties of a ratio add.
            mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);
            spreadBps += spreadBps2;
        }

        return _computeBidAsk(mid, spreadBps);
    }
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L156-194)
```text
    function createAnchoredProvider(
        address oracle,
        bytes32 baseFeedId,
        bytes32 quoteFeedId,
        uint256 minMargin,
        uint256 maxRefStaleness,
        uint16  maxSpreadBps,
        bool    mutableParams,
        int256  marginStep,
        address baseToken,
        address quoteToken
    ) external override returns (address provider) {
        if (!_oracles.contains(oracle)) revert OracleNotAllowed(oracle);

        // Feeds without an explicit class fall back to the admin-configured DEFAULT_CLASS envelope.
        bytes32 classId = feedClass[baseFeedId];
        if (classId == bytes32(0)) classId = DEFAULT_CLASS;

        Envelope storage env = envelopes[classId];
        if (!env.exists) revert EnvelopeNotFound(classId);
        if (
            minMargin < env.minMarginMin || minMargin > env.minMarginMax
            || maxRefStaleness < env.stalenessMin || maxRefStaleness > env.stalenessMax
            || maxSpreadBps < env.maxSpreadMin || maxSpreadBps > env.maxSpreadMax
        ) revert ParamsOutOfEnvelope();

        AnchoredPriceProvider p = new AnchoredPriceProvider(
            address(this),
            oracle,
            baseFeedId,
            quoteFeedId,
            minMargin,
            maxRefStaleness,
            maxSpreadBps,
            mutableParams,
            marginStep,
            baseToken,
            quoteToken
        );
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L541-546)
```text
  function _validatePriceProvider(address token0, address token1, address priceProvider) internal view {
    if (priceProvider == address(0)) revert InvalidPriceProvider();
    if (IPriceProvider(priceProvider).token0() != token0 || IPriceProvider(priceProvider).token1() != token1) {
      revert PriceProviderTokenMismatch();
    }
  }
```

**File:** metric-core/contracts/MetricOmmPoolFactory.sol (L548-563)
```text
  function _validatePoolParameters(PoolParameters calldata params) internal view {
    if (params.token0 == address(0) || params.token1 == address(0) || params.token0 == params.token1) {
      revert InvalidTokenConfig();
    }
    if (params.admin == address(0)) revert InvalidAdmin();
    _validatePriceProvider(params.token0, params.token1, params.priceProvider);
    if (params.adminFeeDestination == address(0)) revert InvalidAdminFeeDestination();
    if (spreadProtocolFeeE6 > maxProtocolSpreadFeeE6) revert ProtocolFeeTooHigh();
    if (protocolNotionalFeeE8 > maxProtocolNotionalFeeE8) revert ProtocolFeeTooHigh();
    if (params.adminSpreadFeeE6 > maxAdminSpreadFeeE6) revert AdminFeeTooHigh();
    if (params.adminNotionalFeeE8 > maxAdminNotionalFeeE8) revert AdminFeeTooHigh();
    if (params.initialAmount0PerShareE18 == 0 || params.initialAmount1PerShareE18 == 0) {
      revert InvalidInitialAmount();
    }
    if (params.minimalMintableLiquidity == 0) revert InvalidMinimalMintableLiquidity();
  }
```
