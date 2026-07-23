### Title
Unhandled revert in `IPricedOracle.price()` call permanently bricks pool swaps — (`smart-contracts-poc/contracts/PriceProvider.sol`, `ProtectedPriceProvider.sol`, `AnchoredPriceProvider.sol`)

---

### Summary

All three price-provider contracts call `IPricedOracle(address(offchainOracle)).price(feedId, pool)` as a bare external call with no `try/catch` wrapper. If the oracle contract reverts for any reason (upgrade, deprecation, access-control change, or internal panic), the revert propagates through `getBidAndAskPrice()` into the pool's swap path, permanently halting all swaps for every pool bound to that provider. Because `offchainOracle` and the feed IDs are `immutable`, there is no in-place recovery path.

---

### Finding Description

In `PriceProvider._getBidAndAskPrice()`:

```solidity
(uint256 mid, uint256 spread, , uint256 refTime) =
    IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);
```

In `ProtectedPriceProvider._getBidAndAskPrice()`:

```solidity
(uint256 mid, uint256 spread, , uint256 refTime) =
    IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);
```

In `AnchoredPriceProvider._readLeg()`:

```solidity
(mid, spreadBps, , refTime) = IPricedOracle(address(offchainOracle)).price(feedId, msg.sender);
```

All three are bare calls. A revert from the oracle propagates uncaught to the pool's swap dispatcher. The pool has no fallback; the swap reverts entirely.

The codebase already demonstrates awareness of this pattern: `AnchoredPriceProvider._readSource()` uses a gas-bounded assembly `staticcall` precisely to absorb source reverts without propagating them. That same discipline was not applied to the oracle call itself.

The oracle address and feed IDs are `immutable` in every provider variant:

- `PriceProvider`: `offchainOracle` (immutable), `offchainFeedId` (immutable)
- `ProtectedPriceProvider`: `offchainOracle` (immutable), `offchainFeedId` (immutable)
- `AnchoredPriceProvider`: `offchainOracle` (immutable), `baseFeedId` (immutable), `quoteFeedId` (immutable)

There is no setter for any of these. Once the oracle reverts, the provider is permanently bricked.

---

### Impact Explanation

Every pool swap calls `getBidAndAskPrice()` on its price provider. If the oracle reverts, `getBidAndAskPrice()` reverts, and the pool's swap reverts. No swap can execute. LP positions cannot be rebalanced. Traders cannot exit positions. This is a complete, permanent functional DoS of the swap path for all pools sharing the affected oracle/provider binding — matching the "broken core pool functionality causing unusable swap/liquidity flows" impact gate.

---

### Likelihood Explanation

Pyth and Chainlink Data Streams are live, evolving systems. Feed IDs are deprecated and replaced; oracle contracts are upgraded; access-control lists are modified. Any of these events can cause `price()` to revert. Because the oracle address and feed ID are immutable in the provider, there is no administrative recovery path short of deploying a new provider and migrating all pools — which itself requires pool-level admin action and may not be possible if the pool's provider slot is also immutable. The scenario is not hypothetical: Chainlink's multisig can block feed access at will, and Pyth has deprecated feed IDs in the past.

---

### Recommendation

Wrap the oracle call in a `try/catch` (or an assembly-level call with success check, mirroring `_readSource`) and return the fail-closed sentinel `(0, type(uint128).max)` on revert. Example for `PriceProvider._getBidAndAskPrice()`:

```solidity
function _getBidAndAskPrice() internal returns (uint128, uint128) {
    uint256 mid; uint256 spread; uint256 refTime;
    try IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender)
        returns (uint256 _mid, uint256 _spread, uint256, uint256 _refTime)
    {
        mid = _mid; spread = _spread; refTime = _refTime;
    } catch {
        return (0, type(uint128).max); // fail closed
    }
    // ... existing staleness / validity checks ...
}
```

Apply the same pattern to `ProtectedPriceProvider._getBidAndAskPrice()` and `AnchoredPriceProvider._readLeg()`. This ensures a reverted oracle degrades gracefully to a stalled-feed signal rather than a hard revert, preserving LP withdrawal paths and preventing permanent swap DoS.

---

### Proof of Concept

1. Deploy `PriceProvider` with a live Pyth oracle address and feed ID (both immutable).
2. Pyth deprecates the feed ID or upgrades the oracle contract such that `price(feedId, pool)` reverts.
3. Any call to `pool.swap(...)` → `provider.getBidAndAskPrice()` → `_getBidAndAskPrice()` → `oracle.price(...)` reverts.
4. All swaps on the pool revert permanently. No setter exists to point the provider at a new oracle. The pool is bricked.

Relevant code locations: [1](#0-0) [2](#0-1) [3](#0-2) 

Immutability of oracle binding (no recovery path): [4](#0-3) [5](#0-4) 

Contrast with the protected source call (revert-safe assembly pattern already used in the same codebase): [6](#0-5)

### Citations

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L30-32)
```text
    IOffchainOracle public immutable offchainOracle;
    bytes32         public immutable offchainFeedId;
    address         public immutable factory;
```

**File:** smart-contracts-poc/contracts/PriceProvider.sol (L193-196)
```text
        //    refTime is already in seconds.
        (uint256 mid, uint256 spread, , uint256 refTime) =
            IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);

```

**File:** smart-contracts-poc/contracts/ProtectedPriceProvider.sol (L181-184)
```text
    function _getBidAndAskPrice() internal returns (uint128, uint128) {
        (uint256 mid, uint256 spread, , uint256 refTime) =
            IPricedOracle(address(offchainOracle)).price(offchainFeedId, msg.sender);
        return _computeBidAsk(mid, spread, refTime);
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L65-69)
```text
    IOffchainOracle public immutable offchainOracle;
    bytes32         public immutable baseFeedId;
    /// @notice Optional second feed for synthetic ratio quoting; zero = single-feed (no conversion).
    ///         Synthetic mid = price(baseFeedId) / price(quoteFeedId), e.g. BTC/USD ÷ ETH/USD = BTC/ETH.
    bytes32         public immutable quoteFeedId;
```

**File:** smart-contracts-poc/contracts/AnchoredPriceProvider.sol (L279-281)
```text
    {
        (mid, spreadBps, , refTime) = IPricedOracle(address(offchainOracle)).price(feedId, msg.sender);

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
