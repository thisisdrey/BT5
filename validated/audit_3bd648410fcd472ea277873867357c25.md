### Title
Unvalidated `quoteFeedId` in `AnchoredProviderFactory.createAnchoredProvider` Bypasses Envelope Bounds, Enabling Stale Synthetic-Ratio Prices to Reach Pool Swaps — (`smart-contracts-poc/contracts/AnchoredProviderFactory.sol`)

---

### Summary

`AnchoredProviderFactory.createAnchoredProvider` enforces admin-curated envelope bounds (`minMargin`, `maxRefStaleness`, `maxSpreadBps`) exclusively against `baseFeedId`'s class. The `quoteFeedId` — the second leg of a synthetic ratio quote — is passed directly to the `AnchoredPriceProvider` constructor with **no envelope validation whatsoever**. Because `MAX_REF_STALENESS` is chosen and validated only for the base-feed class, a `quoteFeedId` whose oracle updates less frequently than that staleness window can produce a stale denominator price that passes every per-leg guard and flows into the pool's bid/ask computation.

---

### Finding Description

In `AnchoredProviderFactory.createAnchoredProvider`, the envelope lookup and parameter check are keyed exclusively on `baseFeedId`:

```solidity
bytes32 classId = feedClass[baseFeedId];          // only baseFeedId
if (classId == bytes32(0)) classId = DEFAULT_CLASS;
Envelope storage env = envelopes[classId];
if (!env.exists) revert EnvelopeNotFound(classId);
if (
    minMargin < env.minMarginMin || minMargin > env.minMarginMax
    || maxRefStaleness < env.stalenessMin || maxRefStaleness > env.stalenessMax
    || maxSpreadBps < env.maxSpreadMin || maxSpreadBps > env.maxSpreadMax
) revert ParamsOutOfEnvelope();
``` [1](#0-0) 

`quoteFeedId` is then forwarded to the constructor without any class lookup, envelope existence check, or parameter-range validation:

```solidity
AnchoredPriceProvider p = new AnchoredPriceProvider(
    address(this), oracle,
    baseFeedId,
    quoteFeedId,   // ← no envelope check
    minMargin, maxRefStaleness, maxSpreadBps,
    ...
);
``` [2](#0-1) 

Inside `AnchoredPriceProvider._getBidAndAskPrice`, both legs are read through `_readLeg`, which applies the **same** `MAX_REF_STALENESS` (validated only for the base class) to the quote leg:

```solidity
(uint256 mid2, uint256 spreadBps2, , bool ok2) = _readLeg(_quote);
if (!ok2 || mid2 == 0) return (0, type(uint128).max);
mid = Math.mulDiv(mid, ORACLE_DECIMALS, mid2);   // synthetic ratio
spreadBps += spreadBps2;
``` [3](#0-2) 

`_readLeg` accepts a price as fresh if `(block.timestamp - refTime) <= MAX_REF_STALENESS`:

```solidity
if (_isStale(refTime, block.timestamp, MAX_REF_STALENESS)) return (mid, spreadBps, refTime, false);
``` [4](#0-3) 

`MAX_REF_STALENESS` was validated against the **base-feed** envelope only. If the quote feed belongs to a class that requires a tighter staleness bound (e.g., a slower-updating RWA or exotic asset), its price can be stale by up to `MAX_REF_STALENESS` seconds and still pass the guard. The stale denominator then corrupts the synthetic mid, which flows unchecked into `_computeBidAsk` and ultimately into the pool's bid/ask prices used for swap execution.

The factory's own eligibility predicate — `isProvider(p)` — returns `true` for any provider deployed through `createAnchoredProvider`, so the corrupted provider is indistinguishable from a correctly configured one:

```solidity
function isProvider(address provider) external view returns (bool) {
    return _providers.contains(provider);
}
``` [5](#0-4) 

---

### Impact Explanation

A stale `quoteFeedId` price in the denominator of the synthetic ratio shifts the mid price by the percentage move of the quote asset during the staleness window. For example, if the quote asset moves 5 % while its price is stale, the synthetic mid is 5 % off. Arbitrageurs can then swap against the pool at the wrong price, extracting value directly from LP balances. This is a **direct loss of LP principal** — a swap conservation failure where the pool receives less input than the oracle-derived price requires.

---

### Likelihood Explanation

`createAnchoredProvider` is **permissionless**; any address can call it. A creator need only supply a legitimate `baseFeedId` (to pass the envelope check) and pair it with any `quoteFeedId` — including one whose oracle updates far less frequently than `MAX_REF_STALENESS`. Because the resulting provider passes `isProvider()`, it can be attached to a public pool. The staleness window opens naturally whenever the quote oracle's heartbeat exceeds the base-class staleness bound, requiring no special on-chain action from the attacker beyond the initial provider creation.

---

### Recommendation

Apply envelope validation to `quoteFeedId` symmetrically with `baseFeedId`. At minimum:

1. Look up `feedClass[quoteFeedId]` and resolve its envelope (falling back to `DEFAULT_CLASS`).
2. Require that the chosen `maxRefStaleness` satisfies **both** envelopes: `max(env_base.stalenessMin, env_quote.stalenessMin) ≤ maxRefStaleness ≤ min(env_base.stalenessMax, env_quote.stalenessMax)`.
3. Apply the same intersection logic to `maxSpreadBps` and `minMargin`.

If the two envelopes are incompatible (no valid intersection), revert with a descriptive error rather than silently accepting the provider.

---

### Proof of Concept

1. Admin configures `DEFAULT_CLASS` envelope: `stalenessMax = 3600` (1 hour), `maxSpreadMax = 200` (2 %).
2. Attacker calls `createAnchoredProvider(oracle, BTC_USD_FEED, SLOW_RWA_FEED, minMargin=0, maxRefStaleness=3600, maxSpreadBps=200, ...)`.
   - `feedClass[BTC_USD_FEED]` resolves to `DEFAULT_CLASS`; envelope check passes.
   - `SLOW_RWA_FEED` (updates every 4 hours) is never checked against any envelope.
   - Provider is deployed; `isProvider(provider) == true`.
3. Attacker (or any user) creates a pool with this provider.
4. `SLOW_RWA_FEED` price is 3 hours old (within `MAX_REF_STALENESS = 3600`? No — 3 hours > 1 hour). Let me correct: if `MAX_REF_STALENESS = 7200` (2 hours, still within the envelope's `stalenessMax = 3600`? No).

   Corrected scenario: Admin envelope allows `stalenessMax = 7200` (2 hours). Attacker sets `maxRefStaleness = 7200`. `SLOW_RWA_FEED` updates every 3 hours. After 100 minutes, `SLOW_RWA_FEED` is 100 minutes stale — within the 2-hour window — but the underlying asset moved 8 %. `_readLeg` accepts the stale price (`100*60 <= 7200`). Synthetic mid is 8 % off. Arbitrageur swaps against the pool, extracting 8 % of the traded notional from LP balances. [6](#0-5) [7](#0-6) [8](#0-7)

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

**File:** smart-contracts-poc/contracts/AnchoredProviderFactory.sol (L281-283)
```text
    function isProvider(address provider) external view returns (bool) {
        return _providers.contains(provider);
    }
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
