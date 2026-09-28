### Title
Precision-loss rounding in `RewardsDistributor` index updates permanently strands accrued reward tokens and can be amplified by permissionless index pokes - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenIndex` computes the per-token reward index delta as `_tokensAccrued.wadDiv(_totalSupply)`, which truncates toward zero. The truncated remainder is never carried forward, while `timestamp` is always advanced. Because `updateBeforeMintOrBurn` is explicitly permissionless ("may be called by anyone"), an unprivileged attacker can poke the index every block to keep `dt` minimal, amplifying the rounding loss to the maximum on every block, permanently reducing claimable `tokensAccruedOf` for all depositors/debtors.

### Finding Description
In `contracts/RewardsDistributor.sol`, `_calculateTokenIndex` computes:

```solidity
uint256 _tokensAccrued = _deltaTimestamps * _speed;
uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
_newIndex = (_supplyState.index + _ratio).toUint224();
_newTimestamp = block.timestamp.toUint32();
``` [1](#0-0) 

`wadDiv` computes `floor(_tokensAccrued * 1e18 / _totalSupply)`. The residual `_tokensAccrued * 1e18 % _totalSupply` is discarded, and — critically — `_newTimestamp` is set to `block.timestamp` even though the residual was never accounted. The same happens in the `_speed == 0`/`_deltaTimestamps > 0` branch. The lost amount corresponds to `_ratio` falling short by up to ~1 wad-index unit, i.e. up to ~`_totalSupply / 1e18` reward tokens per update, which then can never accrue to any account since only index deltas convert into `tokensAccruedOf` via `_calculateTokenDelta` (which itself truncates again via `wadMul`). [2](#0-1) 

The attacker entry point is `updateBeforeMintOrBurn`, which the NatSpec states "may be called by anyone to update stored indexes" and has no access control: [3](#0-2) 

`claimRewards` is also permissionless and updates the index the same way. An attacker calls `updateBeforeMintOrBurn(token, attacker)` every block (or sandwiches every user deposit/mint/transfer, which also trigger `_updateTokenIndex`), keeping `dt` at the block-time minimum so that the per-call truncation removes the maximal possible share of emissions. In the extreme case where `dt * speed * 1e18 < totalSupply` (very large token supply relative to emission speed), `_ratio` is 0 forever while the timestamp advances — 100% of emissions are burned. The invariant broken is reward accrual conservation: `sum(tokensAccruedOf)` permanently lags `integral(speed dt)`.

### Impact Explanation
Medium: on every index update for every rewarded token, a fraction of emitted reward tokens becomes permanently unclaimable — it accrues to no account and remains idle in the distributor's `rewardToken` balance. An attacker can deliberately maximize this loss frequency at zero cost beyond gas, and under adverse `speed`/`totalSupply` configurations can suppress reward accrual entirely. This is theft/freezing of unclaimed yield, in-scope per the engagement rules.

### Likelihood Explanation
Medium: the truncation happens on literally every index update, so residual loss is continuous. The permissionless `updateBeforeMintOrBurn`/`claimRewards` path lets any EOA force worst-case (minimum-`dt`) rounding each block. The full-grief edge case requires `totalSupply > dt * speed * 1e18`, which needs a large token supply or tiny emission speed — plausible for low-`speed` configs — but not guaranteed on all deployments.

### Recommendation
Store the division remainder per token (e.g., `residualOf[token] += _tokensAccrued * 1e18 % _totalSupply`) and fold it into the numerator on the next `_calculateTokenIndex` call, so truncation is bounded to a single sub-unit event rather than once per update. Alternatively, accumulate elapsed time in a high-precision accumulator instead of resetting `timestamp` when `_ratio == 0` (i.e., only advance `timestamp` proportional to the accounted portion).

### Proof of Concept
Foundry/Hardhat sketch mirroring `test/RewardDistributor.test.ts` setup:

```solidity
function test_indexPrecisionGrief() public {
    // speed = 1 token/sec, deposit token supply large
    vm.prank(governor);
    distributor.updateTokenSpeed(address(depositToken), 1 ether);

    uint256 supply = 1_000_000 ether; // e.g. msToken supply
    depositToken.mint(alice, supply); // alice holds all supply

    // Attacker pokes the index every block to keep dt minimal
    for (uint256 i; i < 1000; i++) {
        vm.warp(block.timestamp + 1); // 1s dt
        vm.roll(block.number + 1);
        // permissionless call; rounds down dt*speed*1e18/totalSupply each time
        distributor.updateBeforeMintOrBurn(depositToken, attacker);
    }

    // claimable strictly less than 1000 * speed due to per-call truncation;
    // if supply > 1e36 the index never moves at all.
    uint256 accrued = distributor.tokensAccruedOf(alice);
    assertLt(accrued, 1000 ether);
}
```

Note: even where `dt * speed * 1e18 >= totalSupply`, each poke truncates up to `totalSupply/1e18` tokens of emissions; forcing minimum `dt` each block maximizes the number of truncation events per unit time. A fork test can measure the drift between `integral(speed dt)` and `sum(tokensAccruedOf)` with and without the poking pattern.

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

**File:** contracts/RewardsDistributor.sol (L201-211)
```text
        uint256 _speed = tokenSpeeds[token_];
        uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
        if (_deltaTimestamps > 0 && _speed > 0) {
            uint256 _totalSupply = token_.totalSupply();
            uint256 _tokensAccrued = _deltaTimestamps * _speed;
            uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
            _newIndex = (_supplyState.index + _ratio).toUint224();
            _newTimestamp = block.timestamp.toUint32();
        } else if (_deltaTimestamps > 0 && _supplyState.index > 0) {
            _newTimestamp = block.timestamp.toUint32();
        }
```

**File:** contracts/RewardsDistributor.sol (L229-231)
```text
        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
    }
```
