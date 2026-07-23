### Title
`PriceVelocityGuardExtension` uses `block.number` for velocity timing, rendering the guard ineffective on L2 chains — (File: `metric-periphery/contracts/extensions/PriceVelocityGuardExtension.sol`)

---

### Summary

`PriceVelocityGuardExtension.beforeSwap` measures elapsed time between price updates using `block.number`. On L2 chains (Optimism, Base, Arbitrum), where each transaction occupies its own block and blocks are produced at sub-second to 2-second intervals rather than Ethereum's ~12-second cadence, the `blockDiff` between consecutive swaps is always 1 regardless of real elapsed time. This makes the velocity cap 6–12× more permissive in wall-clock terms than the pool admin intended, allowing rapid oracle-price manipulation that the guard was deployed to prevent.

---

### Finding Description

`PriceVelocityGuardExtension` stores and compares `block.number` to enforce a per-block price-change cap:

```solidity
// metric-periphery/contracts/extensions/PriceVelocityGuardExtension.sol
s.lastUpdateBlock = uint64(block.number);          // line 58
uint256 blockDiff = block.number - prevBlock;       // line 63
uint256 allowedSq = uint256(maxChange) * uint256(maxChange) * (1 + blockDiff); // line 70
```

The allowed deviation formula is:

```
changeE18² ≤ maxChangePerBlockE18² × (1 + blockDiff)
```

On Ethereum mainnet, `blockDiff = 1` between two consecutive swaps represents ~12 seconds of real time. On Optimism/Base, every transaction lands in its own block, so `blockDiff = 1` represents ~2 seconds. The same `maxChangePerBlockE18` value therefore permits the same absolute price movement in 2 seconds that was intended to be the limit for 12 seconds — a 6× relaxation of the guard in real-time terms.

The admin-callable `setLastMidPrice` also stamps `block.number`:

```solidity
// line 32
s.lastUpdateBlock = uint64(block.number);
```

So the reference point is equally miscalibrated.

The protocol explicitly targets L2 deployment: `PriceProviderL2.sol` and `ProtectedPriceProviderL2.sol` exist as first-class production contracts, and `PriceProviderL2` already uses `block.timestamp` correctly for its own staleness check (`_isStale`, line 215). The inconsistency is therefore not theoretical.

---

### Impact Explanation

The velocity guard is the last line of defense against rapid oracle mid-price manipulation reaching pool swaps. When it is 6× too permissive:

- An adversary (or a compromised/manipulated oracle) can move the mid-price by up to `sqrt(6) ≈ 2.45×` the intended per-block cap within a single real-time second on Optimism.
- The pool's `beforeSwap` hook accepts the manipulated bid/ask derived from that mid-price, executing swaps at prices that deviate materially from the intended anchor band.
- LPs bear the loss: the pool settles at a price that is worse than the oracle-enforced fair value, draining LP reserves in favor of the swapper.

This is a **bad-price execution** impact: stale or unclamped bid/ask quotes reach pool swaps because the guard that should have rejected them is miscalibrated.

---

### Likelihood Explanation

- The protocol ships L2-specific price provider contracts, confirming L2 deployment is in scope and planned.
- Any unprivileged swapper triggers `beforeSwap` on every swap; no special role is required.
- The miscalibration is passive and permanent — it requires no attacker setup beyond submitting rapid swaps.
- Pool admins setting `maxChangePerBlockE18` will calibrate for Ethereum block times (12 s), not L2 block times (2 s), because the parameter name and NatSpec reference "blocks" with no L2 caveat.

---

### Recommendation

Replace `block.number` with `block.timestamp` throughout `PriceVelocityGuardExtension`:

```solidity
// Storage field: rename lastUpdateBlock → lastUpdateTime (uint64 seconds)
s.lastUpdateTime = uint64(block.timestamp);

// In beforeSwap:
uint256 timeDiff = block.timestamp - prevTime;   // seconds elapsed
uint256 allowedSq = uint256(maxChange) * uint256(maxChange) * (1 + timeDiff);
```

Rename `maxChangePerBlockE18` → `maxChangePerSecondE18` and update the NatSpec, interface (`IPriceVelocityGuardExtension`), and any existing pool configurations. This mirrors the approach already used in `PriceProviderL2._isStale` and is consistent with the Frankencoin finding's confirmed fix.

---

### Proof of Concept

**Setup**: Deploy on Optimism. Pool admin sets `maxChangePerBlockE18 = X` intending to cap price movement at `X` per ~12-second Ethereum block.

**Attack**:
1. Submit swap at time `T`. `beforeSwap` records `lastUpdateBlock = N`, `lastMidPriceX64 = P`.
2. Submit swap at time `T + 2s`. On Optimism, `block.number = N+1`, so `blockDiff = 1`.
3. Allowed change: `maxChange * sqrt(2)` — identical to Ethereum.
4. But only 2 seconds elapsed, not 12. The guard permits the same absolute price move in 1/6th the real time.
5. Repeat: in 12 seconds (6 Optimism blocks), the cumulative allowed drift is `maxChange * sqrt(7) ≈ 2.65×` the intended single-block cap, versus `maxChange * sqrt(2) ≈ 1.41×` on Ethereum for the same wall-clock period.
6. Each swap executes at the manipulated oracle mid-price, with LP reserves drained by the difference between the manipulated price and fair value. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** metric-periphery/contracts/extensions/PriceVelocityGuardExtension.sol (L29-33)
```text
  function setLastMidPrice(address pool_, uint128 newLastMidPriceX64) external onlyPoolAdmin(pool_) {
    PriceVelocityState storage s = priceVelocityState[pool_];
    s.lastMidPriceX64 = newLastMidPriceX64;
    s.lastUpdateBlock = uint64(block.number);
    emit LastMidPriceUpdated(pool_, newLastMidPriceX64);
```

**File:** metric-periphery/contracts/extensions/PriceVelocityGuardExtension.sol (L55-70)
```text
    uint64 prevBlock = s.lastUpdateBlock;

    s.lastMidPriceX64 = midPrice;
    s.lastUpdateBlock = uint64(block.number);

    if (prevMid != 0) {
      uint64 maxChange = s.maxChangePerBlockE18;
      if (maxChange != 0) {
        uint256 blockDiff = block.number - prevBlock;

        uint256 delta = midPrice > prevMid ? uint256(midPrice - prevMid) : uint256(prevMid - midPrice);

        uint256 changeE18 = (delta * 1e18) / uint256(prevMid);

        uint256 actualSq = changeE18 * changeE18;
        uint256 allowedSq = uint256(maxChange) * uint256(maxChange) * (1 + blockDiff);
```

**File:** smart-contracts-poc/contracts/PriceProviderL2.sol (L213-217)
```text

        // 2. Staleness check
        if (_isStale(refTime, block.timestamp, MAX_TIME_DELTA, FUTURE_TOLERANCE)) {
            return (0, type(uint128).max);
        }
```
