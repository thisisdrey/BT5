### Title
Single `MAX_REF_STALENESS` Applied to Both Synthetic Legs Without Quote-Feed Envelope Validation — (`smart-contracts-poc/contracts/AnchoredProviderFactory.sol`)

### Summary
`AnchoredProviderFactory.createAnchoredProvider` validates `maxRefStaleness` exclusively against the `baseFeedId`'s class envelope. In synthetic (two-feed) mode, `AnchoredPriceProvider._readLeg` applies the same `MAX_REF_STALENESS` to **both** `baseFeedId` and `quoteFeedId`. When the two feeds have different oracle heartbeats, the single staleness bound is misconfigured for one leg, allowing a stale price to pass and corrupt the synthetic ratio that reaches pool swaps.

### Finding Description

**Factory validation — base feed only:**

In `AnchoredProviderFactory.createAnchoredProvider`, the envelope lookup is keyed exclusively on `baseFeedId`:

```solidity
bytes32 classId = feedClass[baseFeedId];   // quoteFeedId never consulted
if (classId == bytes32(0)) classId = DEFAULT_CLASS;
Envelope storage env = envelopes[classId];
if (
    ...
    || maxRefStaleness < env.stalenessMin || maxRefStaleness > env.stalenessMax
    ...
) revert ParamsOutOfEnvelope();
``` [1](#0-0) 

`quoteFeedId` is passed straight through to the provider constructor with zero envelope validation. [2](#0-1) 

**Both legs checked against the same immutable:**

At read time, `_readLeg` applies `MAX_REF_STALENESS` identically to both feeds:

```solidity
function _readLeg(bytes32 feedId) internal returns (...) {
    (mid, spreadBps, , refTime) = IPricedOracle(...).price(feedId, msg.sender);
    if (_isStale(refTime, block.timestamp, MAX_REF_STALENESS))   // same bound for both legs
        return (mid, spreadBps, refTime, false);
    ...
}
``` [3](#0-2) 

**Synthetic ratio construction:**

```solidity
(uint256 mid, uint256 spreadBps, , bool ok) = _readLeg(baseFeedId);
...
(uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLeg(_quote);
mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);   // ratio of the two legs
``` [4](#0-3) 

### Impact Explanation

**Concrete scenario — stale fast-feed price accepted:**

1. Creator calls `createAnchoredProvider(oracle, USDC_USD_FEED, ETH_USD_FEED, ..., maxRefStaleness = 86_400, ...)`.
2. Factory looks up `feedClass[USDC_USD_FEED]` → USDC class envelope allows `stalenessMax = 86_400 s` (matching USDC/USD's 24 h Chainlink heartbeat). Validation passes.
3. Provider is deployed with `MAX_REF_STALENESS = 86_400 s` and `quoteFeedId = ETH_USD_FEED` (3 600 s heartbeat).
4. At swap time, `_readLeg(ETH_USD_FEED)` accepts an ETH/USD price that is up to 24 h old — far beyond ETH/USD's 1 h heartbeat.
5. Synthetic mid = USDC/USD ÷ ETH/USD = 1 / (stale ETH price). If ETH moved 15 % since the stale update, the synthetic ratio is off by 15 %.
6. `_computeBidAsk` builds the band from this corrupted mid. The band clamp cannot help: the stale price **is** the anchor — there is no fresher reference to clamp against.
7. The corrupted bid/ask is returned to the pool and drives `SwapMath.computeSwapStep`. Traders receive 15 % more output tokens than the current market warrants; the pool pays out LP assets it is not owed.

`createAnchoredProvider` is permissionless — any address can deploy a provider that passes `isProvider()` and is eligible for use in a public pool. [5](#0-4) 

### Likelihood Explanation

- `createAnchoredProvider` is fully permissionless; no admin approval is required after the oracle allow-list and envelopes are configured.
- The factory's own NatSpec states "the audit-once bound is never bypassed," but the bound is only enforced for `baseFeedId`. A creator who sets `maxRefStaleness` to match the slower feed's heartbeat (a natural, non-malicious choice) silently misconfigures the faster quote feed.
- Chainlink feeds used in the protocol have materially different heartbeats (1 h for ETH/BTC, 24 h for stablecoins), making the mismatch likely in any stablecoin-denominated synthetic pair.

### Recommendation

In `createAnchoredProvider`, when `quoteFeedId != bytes32(0)`, look up the quote feed's class envelope and verify that `maxRefStaleness` also satisfies that envelope's staleness bounds:

```solidity
if (quoteFeedId != bytes32(0)) {
    bytes32 quoteClassId = feedClass[quoteFeedId];
    if (quoteClassId == bytes32(0)) quoteClassId = DEFAULT_CLASS;
    Envelope storage qEnv = envelopes[quoteClassId];
    if (!qEnv.exists) revert EnvelopeNotFound(quoteClassId);
    if (maxRefStaleness < qEnv.stalenessMin || maxRefStaleness > qEnv.stalenessMax)
        revert ParamsOutOfEnvelope();
}
```

Alternatively, store separate per-leg staleness values in the provider and validate each independently.

### Proof of Concept

```
State:
  feedClass[USDC_USD_FEED] = STABLECOIN_CLASS
  envelopes[STABLECOIN_CLASS].stalenessMax = 86_400   // 24 h
  feedClass[ETH_USD_FEED]  = MAJORS_CLASS
  envelopes[MAJORS_CLASS].stalenessMax  = 3_600       // 1 h

Step 1 — permissionless provider creation:
  createAnchoredProvider(
      oracle,
      baseFeedId  = USDC_USD_FEED,   // class → STABLECOIN, stalenessMax 86400 ✓
      quoteFeedId = ETH_USD_FEED,    // class → never checked
      maxRefStaleness = 86_400,
      ...
  )
  → provider deployed, isProvider() == true

Step 2 — pool uses provider; ETH/USD feed goes 23 h without update:
  oracle.price(ETH_USD_FEED, pool)
    → refTime = block.timestamp - 82_800   (23 h ago)
    → _isStale(82800, now, 86400) == false  ← stale price accepted

Step 3 — corrupted synthetic ratio:
  mid = USDC_USD / ETH_USD_stale
      = 1e8 / 3_000e8   (ETH was $3 000 23 h ago; now $2 500)
  actual mid should be 1e8 / 2_500e8

Step 4 — pool swap executes at stale bid/ask:
  Trader sells USDC, receives ETH priced at $3 000 instead of $2 500
  Pool overpays by 20 % of the swap value → LP principal drained
``` [3](#0-2) [6](#0-5)

### Citations

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

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L258-271)
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
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L277-295)
```text
    function _readLeg(bytes32 feedId)
        internal returns (uint256 mid, uint256 spreadBps, uint256 refTime, bool ok)
    {
        (mid, spreadBps, , refTime) = IPricedOracle(address(offchainOracle)).price(feedId, msg.sender);

        // Stale reference → not ok. Clamping to a stale anchor is the one false-safety case.
        if (_isStale(refTime, block.timestamp, MAX_REF_STALENESS)) return (mid, spreadBps, refTime, false);

        // Basic validity — mid positive, spreadBps not the stalled/off-hours marker (the Chainlink oracle
        // writes spreadBps = ORACLE_BPS when an RWA market is closed).
        if (mid == 0 || spreadBps >= ORACLE_BPS) return (mid, spreadBps, refTime, false);

        // Per-leg price guard.
        (uint128 guardMin, uint128 guardMax) = offchainOracle.priceGuard(feedId);
        guardMax = guardMax == 0 ? type(uint128).max : guardMax;
        if (mid < guardMin || mid > guardMax) return (mid, spreadBps, refTime, false);

        ok = true;
    }
```
