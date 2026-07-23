### Title
Synthetic-Mode Provider Accepts `baseFeedId == quoteFeedId`, Fixing Pool Price at 1.0 and Doubling Spread — (`smart-contracts-poc/contracts/AnchoredPriceProvider.sol`, `smart-contracts-poc/contracts/AnchoredProviderFactory.sol`)

---

### Summary

`AnchoredPriceProvider` and `AnchoredProviderFactory.createAnchoredProvider` both accept `quoteFeedId == baseFeedId` without any guard. When the same feed ID is supplied for both legs of the synthetic ratio, the computed mid collapses to the constant `ORACLE_DECIMALS` (1e8 = 1.0 in 8-decimal format) and the spread is doubled. Any pool using such a provider quotes a price of ≈ 1.0 token0-per-token1 regardless of the real market price, enabling traders to drain LP reserves at a fraction of fair value.

---

### Finding Description

`_getBidAndAskPrice` in synthetic mode reads both legs independently and computes:

```
mid  = Math.mulDiv(mid1, ORACLE_DECIMALS, mid2)   // mid1/mid2
spreadBps += spreadBps2                            // per-leg spreads added
``` [1](#0-0) 

When `baseFeedId == quoteFeedId`, both `_readLeg` calls return the same `(mid1, spreadBps1)`. The ratio becomes:

```
mid = Math.mulDiv(P, 1e8, P) = 1e8   // always 1.0, independent of P
spreadBps = 2 * spreadBps1            // doubled
```

Neither the constructor nor the factory checks `baseFeedId != quoteFeedId`:

**Constructor** — only validates tokens, staleness, spread, and marginStep bounds: [2](#0-1) 

**Factory** — validates oracle allow-list and envelope bounds, but not

### Citations

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L136-172)
```text
        require(_factory != address(0));
        factory = _factory;

        offchainOracle = IOffchainOracle(_oracle);
        baseFeedId = _baseFeedId;
        quoteFeedId = _quoteFeedId;

        // Tokens live ONLY here (the oracles are token-free): the pair is an explicit,
        // mandatory input — including the synthetic (two-feed) mode, where the factory
        // knows the pair when it creates the pool.
        require(_baseToken != address(0) && _quoteToken != address(0) && _baseToken != _quoteToken);
        baseToken = _baseToken;
        quoteToken = _quoteToken;

        if (_maxRefStaleness > 7 days) revert MaxRefStalenessOutOfBounds(); // 0 allowed = same-block reference
        MAX_REF_STALENESS = _maxRefStaleness;

        if (_maxSpreadBps == 0 || _maxSpreadBps >= ORACLE_BPS) revert MaxSpreadOutOfBounds();
        MAX_SPREAD_BPS = _maxSpreadBps;

        // minMargin 0 is allowed: the band then relies purely on the oracle spreadBps. If spreadBps is
        // also 0 the band degenerates and the read halts via the refBid >= refAsk guard in _computeBidAsk
        // (never a tighter-than-band quote) — the clamp + that halt are the safety net, not a positive floor.
        // Worst-case half-width must stay below 100% so the clamped bid is always positive.
        if (uint256(_maxSpreadBps) * ONE_BPS_E18 + _minMargin >= BPS_BASE_U) revert BandTooWide();
        minMargin = _minMargin;

        MUTABLE_PARAMS = _mutableParams;
        // marginStep bias + derived step factors (immutable). The customizable variant shapes the quote
        // with confidence then this fixed bias; the load-bearing band clamp in _computeBidAsk keeps the
        // final quote no tighter than the band edge for ANY marginStep sign (a negative value tightens or
        // inverts the pre-clamp quote; the clamp neutralizes it). The immutable variant ignores them.
        if (_marginStep <= -BPS_BASE || _marginStep >= BPS_BASE) revert MarginStepOutOfBounds();
        marginStep = _marginStep;
        stepBidFactor = uint256(BPS_BASE - _marginStep);
        stepAskFactor = uint256(BPS_BASE + _marginStep);
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
