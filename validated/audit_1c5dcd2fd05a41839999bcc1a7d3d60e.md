### Title
`AnchoredPriceProvider` Uses L1-Only Staleness Check, Breaking Swap Execution on L2 Deployments (Base) — (`smart-contracts-poc/contracts/AnchoredPriceProvider.sol`)

---

### Summary

`AnchoredPriceProvider` is the designated standard provider for public pools and is deployed on Base (L2). Its `_isStale()` function is explicitly L1-only: it unconditionally rejects any oracle `refTime` that is ahead of `block.timestamp`. On L2 chains, sequencer clock skew routinely causes oracle timestamps to be slightly ahead of the EVM's `block.timestamp`. This causes `getBidAndAskPrice()` to revert with `FeedStalled` during normal L2 operation, making pool swaps unusable.

---

### Finding Description

The protocol explicitly targets Base (L2) as a deployment chain. The `AnchoredPriceProvider` is described as "the one standard provider for public pools."

Its staleness check is:

```solidity
/// @dev Pure staleness check (L1). Any future refTime is stale.
function _isStale(
    uint256 refTime,
    uint256 nowTs,
    uint256 maxDelta
) internal pure returns (bool) {
    if (refTime == 0) return true;
    if (refTime > nowTs) return true;   // ← rejects any future timestamp
    return (nowTs - refTime) > maxDelta;
}
``` [1](#0-0) 

This is called from `_readLeg()`, which feeds `_getBidAndAskPrice()`, which feeds the public `getBidAndAskPrice()`:

```solidity
if (_isStale(refTime, block.timestamp, MAX_REF_STALENESS)) return (mid, spreadBps, refTime, false);
``` [2](#0-1) 

When `_readLeg` returns `ok = false`, `_getBidAndAskPrice` returns the stall sentinel `(0, type(uint128).max)`, and `getBidAndAskPrice` reverts with `FeedStalled`:

```solidity
function getBidAndAskPrice() external override returns (uint128 bid, uint128 ask) {
    (bid, ask) = _getBidAndAskPrice();
    if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
}
``` [3](#0-2) 

The codebase already recognizes this L2 timing problem. Both `PriceProviderL2` and `ProtectedPriceProviderL2` carry an immutable `FUTURE_TOLERANCE` parameter and an L2-aware `_isStale()` that tolerates oracle timestamps slightly ahead of `block.timestamp`:

```solidity
/// @dev L2 sequencer timestamp can lag behind oracle publication time.
///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
uint256 public immutable FUTURE_TOLERANCE;
``` [4](#0-3) 

```solidity
if (refTime > nowTs) {
    // refTime in the future: tolerate only within futureTol
    return (refTime - nowTs) > futureTol;
}
``` [5](#0-4) 

`AnchoredPriceProvider` has no such accommodation. The `AnchoredProviderFactory.createAnchoredProvider()` accepts no `futureTolerance` parameter and passes none to the provider constructor: [6](#0-5) 

---

### Impact Explanation

When the L2 sequencer's `block.timestamp` lags behind the oracle keeper's wall-clock time (a normal condition on Base/Optimism), the oracle's stored `refTime` will be slightly ahead of `block.timestamp`. `AnchoredPriceProvider._isStale()` treats this as stale, causing `getBidAndAskPrice()` to revert with `FeedStalled` on every swap attempt. Pool swaps are completely blocked for the duration of the skew window. This is broken core pool functionality — the swap flow is unusable — matching the allowed impact gate.

---

### Likelihood Explanation

Base (Optimism stack) sequencer clock skew is a documented, recurring phenomenon. The protocol's own L2 provider variants (`PriceProviderL2`, `ProtectedPriceProviderL2`) were built specifically to handle it. The `AnchoredPriceProvider` is the standard provider for public pools on all deployment chains including Base. Any oracle update pushed by a keeper with a timestamp even 1 second ahead of the sequencer's `block.timestamp` triggers the failure. This is a medium-likelihood event during normal operation.

---

### Recommendation

Add a `futureTolerance` immutable to `AnchoredPriceProvider` (mirroring `PriceProviderL2`) and update `_isStale()` to accept the four-argument L2-aware signature:

```solidity
uint256 public immutable FUTURE_TOLERANCE;

function _isStale(uint256 refTime, uint256 nowTs, uint256 maxDelta, uint256 futureTol)
    internal pure returns (bool)
{
    if (refTime == 0) return true;
    if (refTime > nowTs) return (refTime - nowTs) > futureTol;
    return (nowTs - refTime) > maxDelta;
}
```

Pass `futureTolerance` through `AnchoredProviderFactory.createAnchoredProvider()` and validate it against the envelope (e.g., `<= 1 hours`, matching the L2 provider cap).

---

### Proof of Concept

1. Deploy `AnchoredProviderFactory` + `AnchoredPriceProvider` on Base (or a fork).
2. Push an oracle report with `refTime = block.timestamp + 5` (5-second sequencer skew — within normal Base operation).
3. Call `pool.swap(...)`, which internally calls `provider.getBidAndAskPrice()`.
4. `_readLeg` calls `_isStale(refTime=T+5, nowTs=T, MAX_REF_STALENESS)`.
5. `refTime > nowTs` → returns `true` → `ok = false`.
6. `_getBidAndAskPrice` returns `(0, type(uint128).max)`.
7. `getBidAndAskPrice` reverts `FeedStalled`.
8. Swap reverts. All swaps through this provider are blocked for the duration of the skew.

### Citations

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L214-217)
```text
    function getBidAndAskPrice() external override returns (uint128 bid, uint128 ask) {
        (bid, ask) = _getBidAndAskPrice();
        if (bid == 0 || ask == type(uint128).max) revert FeedStalled();
    }
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L221-230)
```text
    /// @dev Pure staleness check (L1). Any future refTime is stale.
    function _isStale(
        uint256 refTime,
        uint256 nowTs,
        uint256 maxDelta
    ) internal pure returns (bool) {
        if (refTime == 0) return true;
        if (refTime > nowTs) return true;
        return (nowTs - refTime) > maxDelta;
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

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L36-38)
```text
    /// @dev L2 sequencer timestamp can lag behind oracle publication time.
    ///      Allows refTime up to FUTURE_TOLERANCE seconds ahead of block.timestamp.
    uint256 public immutable FUTURE_TOLERANCE;
```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L143-146)
```text
        if (refTime > nowTs) {
            // refTime in the future: tolerate only within futureTol
            return (refTime - nowTs) > futureTol;
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
