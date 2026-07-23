### Title
CREATE-based deployment in price provider factories enables reorg attack to substitute attacker-controlled provider — (`smart-contracts-poc/contracts/PriceProviderFactory.sol`, `PriceProviderFactoryL2.sol`, `AnchoredProviderFactory.sol`)

---

### Summary

All three price-provider factory contracts deploy new providers using the `new` keyword (EVM `CREATE` opcode), whose address depends solely on the factory's nonce. On any EVM chain susceptible to block reorganizations (Arbitrum, Optimism, Polygon, Ethereum mainnet), an attacker can race to occupy the same nonce after a reorg, deploying a provider with malicious parameters at the address a legitimate user expected. The attacker becomes `providerOwner` of that address in the factory's registry. If a pool was configured to use the pre-computed provider address, it will subsequently call `getBidAndAskPrice()` on the attacker's provider, delivering corrupted bid/ask data to every swap.

---

### Finding Description

`PriceProviderFactory.createPriceProvider()` deploys via plain `new`: [1](#0-0) 

`PriceProviderFactoryL2.createPriceProvider()` does the same: [2](#0-1) 

`AnchoredProviderFactory.createAnchoredProvider()` also uses plain `new`: [3](#0-2) 

In all three cases the deployed address is `keccak256(rlp(factory, nonce))` — no `msg.sender` component. Immediately after deployment the factory records: [4](#0-3) 

By contrast, the pool deployer correctly uses `CREATE2` with an explicit salt: [5](#0-4) 

**Attack sequence:**

1. Alice calls `createPriceProvider` with legitimate oracle/feedId/tokens → provider lands at address `P` (factory nonce `N`). Alice (or a pool admin) pre-computes `P` and configures a pool to use it as `priceProvider`.
2. A reorg removes Alice's transaction.
3. Bob calls `createPriceProvider` with a malicious oracle or feedId → provider lands at `P` (same nonce `N`). Bob is recorded as `providerOwner[P]`.
4. Alice's transaction is re-executed → her provider lands at `P+1` (nonce `N+1`). The pool's `priceProvider` field still points to `P`.
5. Every subsequent swap calls `getBidAndAskPrice()` on Bob's provider.

For `PriceProviderFactory`/`PriceProviderFactoryL2` there is no parameter envelope: Bob can supply any oracle address, any feedId, any `marginStep`, and any `maxTimeDelta`. For `AnchoredProviderFactory` the envelope constrains `minMargin`/`maxSpreadBps`/`maxRefStaleness`, but Bob can still choose a different `baseFeedId` (mapping to a different asset), set `mutableParams = true`, and immediately call `setSource` through the factory to install an arbitrary `IAnchorSource`.

The pool reads the provider unconditionally during swaps: [6](#0-5) 

The factory's `isProvider()` predicate returns `true` for Bob's provider because it was legitimately deployed by the factory — no additional guard blocks the corrupted quote from reaching the pool.

---

### Impact Explanation

Bob's provider can return any bid/ask pair that passes the provider's own internal checks (staleness, zero-bid guard). With a wrong oracle or feedId the mid-price is for a completely different asset; with a manipulated `marginStep` the spread is inverted or collapsed. Either way, the pool executes swaps at prices that do not reflect the true market, causing:

- Traders to receive more output tokens than the oracle permits (swap conservation failure).
- LPs to be drained below their fair claim (pool insolvency / loss of LP principal).
- Protocol fee accrual to be based on wrong notional values.

---

### Likelihood Explanation

All three factories are permissionless — no role is required to call `createPriceProvider` / `createAnchoredProvider`. The protocol is explicitly planned for deployment on Arbitrum, Optimism, Polygon, and ZkSync, all of which have documented reorg histories. The attack requires only that the attacker monitors the mempool and submits a competing transaction immediately after a reorg, which is a well-understood MEV strategy. The window is narrow but the incentive (full control of a live pool's price feed) is high.

---

### Recommendation

Deploy providers via `CREATE2` with a salt that encodes `msg.sender` and the full parameter set, for example:

```solidity
bytes32 salt = keccak256(abi.encode(
    msg.sender, _oracle, _feedId, _marginStep, _maxTimeDelta, _baseToken, _quoteToken
));
PriceProvider p = new PriceProvider{salt: salt}(...);
```

This makes the deployed address unique per caller and per parameter set, so a reorg cannot place a different provider at the same address.

---

### Proof of Concept

```
1. Factory nonce = N.
2. Alice: createPriceProvider(legitimateOracle, ETH_USD_FEED, ...) → address P.
3. Pool admin: deployPool(priceProvider = P, ...).
4. Reorg removes both transactions.
5. Bob: createPriceProvider(maliciousOracle, BTC_USD_FEED, ...) → address P (nonce N).
   Factory state: providerOwner[P] = Bob.
6. Alice's tx replayed → createPriceProvider → address P' ≠ P (nonce N+1).
   Pool's priceProvider remains P.
7. Pool.swap() → P.getBidAndAskPrice() → maliciousOracle.price(BTC_USD_FEED, pool)
   → BTC price used for ETH pool → swap executes at ~20× wrong price.
8. Attacker arbitrages the mispriced pool, draining LP funds.
``` [7](#0-6) [8](#0-7) [9](#0-8)

### Citations

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

**File:** smart-contracts-poc/contracts/PriceProviderFactoryL2.sol (L41-79)
```text
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
            _maxTimeDelta,
            _futureTolerance
        );
    }
```

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L156-218)
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

        provider = address(p);
        address creator = msg.sender;

        _providers.add(provider);
        _providersByCreator[creator].add(provider);
        providerOwner[provider] = creator;

        emit ProviderDeployed(
            provider,
            creator,
            baseFeedId,
            quoteFeedId,
            classId,
            p.baseToken(),
            p.quoteToken(),
            minMargin,
            maxRefStaleness,
            maxSpreadBps,
            mutableParams,
            marginStep,
            oracle
        );
    }
```

**File:** metric-core/contracts/MetricOmmPoolDeployer.sol (L61-63)
```text
    pool = address(
      new MetricOmmPool{salt: params.salt}(
        params.factory,
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
