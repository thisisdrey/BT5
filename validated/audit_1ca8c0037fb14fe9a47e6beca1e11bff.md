### Title
Frequent index updates permanently truncate reward accrual to near-zero in `_calculateTokenIndex` - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenIndex()` computes the per-interval reward index delta as `_tokensAccrued.wadDiv(_totalSupply)`, i.e. an integer number of wei-of-index (1e18 scale). Any sub-wei fraction of the index delta is discarded on every update, while the state `timestamp` is still advanced. Because `updateBeforeMintOrBurn()` / `updateBeforeTransfer()` are permissionless and are also invoked on every DepositToken/DebtToken mint, burn, and transfer, an unprivileged attacker can force an index update every block, truncating each interval's accrual. This is the same bug class as the referenced Vyper finding (`amount_claimable_per_share += _amount * PRECISION / totalShares` rounding down): a fixed-precision per-share accumulator whose remainder is lost on every update, making `claimable` systematically under-estimated — or exactly zero when the per-interval ratio is below 1 wei of index.

### Finding Description
In `contracts/RewardsDistributor.sol`:

```solidity
uint256 _tokensAccrued = _deltaTimestamps * _speed;
uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
_newIndex = (_supplyState.index + _ratio).toUint224();
``` [1](#0-0) 

`wadDiv` returns `(a * 1e18 + b/2) / b` — an integer; the fractional part of the true index delta is lost, and `_updateTokenIndex` always writes the new `timestamp` even when `_newIndex == oldIndex` (the `else if (_newTimestamp > 0)` branch), so the elapsed time is consumed while the accrued rewards are dropped. [2](#0-1) 

The function is reachable by anyone: `updateBeforeMintOrBurn(IERC20,address)` is `external` with no access control and explicitly "may be called by anyone to update stored indexes", and it is also triggered internally on every DepositToken/DebtToken mint/burn/transfer. A user calls `updateBeforeMintOrBurn(depositToken, victim)` once per block; each call accrues `deltaT * speed`, divides by `totalSupply` at wad precision, discards the remainder (< 1 wei of index, or the whole delta if `deltaT * speed * 1e18 < totalSupply / 2`), and resets `timestamp`. [3](#0-2) 

### Impact Explanation
The broken invariant is reward accrual conservation: `index(t) - index(t0)` should equal `∫ speed·dt / totalSupply`, but each forced update loses up to ~0.5 wei of index (round-to-nearest truncation of `wadDiv`). With `deltaT * speed * 1e18 / totalSupply` having fractional component f, each update loses f wei of index, i.e. up to ~100% of the per-interval accrual when the true per-interval ratio is a small non-integer (e.g. speed 1e14 wei/s vs totalSupply 1e24 gives true ratio 0.1 wei/s → truncated to 0 forever). Loss = `balanceOf(account) * Σf_i / 1e18` reward tokens, permanently — the remainder time is consumed by the timestamp update and can never be re-accrued. This is theft/freezing of unclaimed yield: the reward tokens that were earmarked for distribution remain locked in the distributor (`_transferRewardIfEnoughTokens` only pays recorded `tokensAccruedOf`).

### Likelihood Explanation
- Fully permissionless trigger (`updateBeforeMintOrBurn` is callable by any EOA; DepositToken/DebtToken transfers also trigger it, so even normal user activity continuously truncates).
- The truncation magnitude is worst exactly when speed is small relative to supply — plausible for low-emission reward streams or large-supply deposit tokens. Even in moderate regimes every forced update bleeds the fractional part of each block's accrual.
- No modifier (reentrancy guard, pause, SynthContext) prevents calling it; the function is designed to be publicly callable.

### Recommendation
Accumulate the remainder instead of discarding it: keep the index denominator implicit by storing the index at higher precision (ray, 1e27) or store a persistent `remainder` per token so `(_tokensAccrued * 1e18 + remainder) / _totalSupply` carries the unallocated fraction forward across updates. Alternatively do not advance `timestamp` unless the full interval accrued, or update the index lazily only when the integral ratio is nonzero while preserving elapsed time accounting.

### Proof of Concept
Hardhat (fork or fixture) sketch against `contracts/RewardsDistributor.sol`:

```ts
// Setup: rewardDistributor initialized, rewardToken (VSP) funded,
// updateTokenSpeed(msdToken, speed) called by governor so index exists.
// msdToken.totalSupply.returns(parseEther('1000000')) // 1e24
// msdToken.balanceOf(alice).returns(parseEther('100'))

const speed = BigNumber.from('100000000000000'); // 1e14 wei/s
await rewardDistributor.connect(governor).updateTokenSpeed(msdToken.address, speed);
await rewardDistributor.updateBeforeMintOrBurn(msdToken.address, alice.address); // index = 1e18

// True ratio per second = 1e14 * 1e18 / 1e24 = 0.1 wei of index -> truncates to 0.
// Attacker griefs: call every block (anyone can).
for (let i = 0; i < 1000; i++) {
  await increaseTimeOfNextBlock(1);
  await rewardDistributor.connect(attackerEOA).updateBeforeMintOrBurn(msdToken.address, alice.address);
}
// ~1000 * 1e14 = 1e17 reward tokens should have accrued but index never moved
const accrued = await rewardDistributor.tokensAccruedOf(alice.address);
expect(accrued).to.eq(0); // all accrual truncated, timestamps consumed
// Even with integer ratio, e.g. speed=1e15 -> true 1.0? choose speed s.t.
// deltaT*speed*1e18/totalSupply = 3.7 -> each update loses 0.7 (~19%).
```

Expected result: `tokensAccruedOf(alice)` is 0 (or materially below `elapsed * speed * balance / totalSupply`), demonstrating permanent loss of unclaimed yield via repeated rounding truncation — the Metronome analog of `amount_claimable_per_share += amount * PRECISION / totalShares` precision loss.

### Citations

**File:** contracts/RewardsDistributor.sol (L175-180)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L203-207)
```text
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
```

**File:** contracts/RewardsDistributor.sol (L271-281)
```text
    function _updateTokenIndex(IERC20 token_) private {
        TokenState storage _supplyState = tokenStates[token_];
        (uint224 _newIndex, uint32 _newTimestamp) = _calculateTokenIndex(_supplyState, token_);
        if (_newIndex > 0 && _newTimestamp > 0) {
            _supplyState.index = _newIndex;
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_newIndex, _newTimestamp);
        } else if (_newTimestamp > 0) {
            _supplyState.timestamp = _newTimestamp;
            emit TokenIndexUpdated(_supplyState.index, _newTimestamp);
        }
```
