### Title
Rewards emitted while a DepositToken/DebtToken has zero total supply are permanently lost - ([File: contracts/RewardsDistributor.sol](https://github.com/Lauraivanka/metronome-synth-public--010/blob/main/contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor` distributes `rewardToken` to holders of each `DepositToken`/`DebtToken` pro-rata via a per-token index advanced by `tokenSpeeds[token_]` emissions per second. When a registered token has `totalSupply() == 0` while its speed is non-zero, `_calculateTokenIndex` still advances the tracked timestamp but adds zero to the index, so all emissions during the zero-supply window are skipped and can never be claimed by anyone. This is the same bug class as the referenced report: rewards accrued "before the first staker" (here, before the first depositor/borrower, or after full withdrawal) are stuck in the contract forever.

### Finding Description
The index update logic lives in `_calculateTokenIndex`: [1](#0-0) 

```solidity
uint256 _deltaTimestamps = block.timestamp - uint256(_supplyState.timestamp);
if (_deltaTimestamps > 0 && _speed > 0) {
    uint256 _totalSupply = token_.totalSupply();
    uint256 _tokensAccrued = _deltaTimestamps * _speed;
    uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
    _newIndex = (_supplyState.index + _ratio).toUint224();
    _newTimestamp = block.timestamp.toUint32();
}
```

When `_totalSupply == 0`, `_ratio` is forced to `0`, yet `_newTimestamp` is still set to `block.timestamp`. `_updateTokenIndex` then persists that timestamp, permanently discarding `deltaTimestamps * speed` worth of emissions: [2](#0-1) 

Nothing prevents this state:

- `_updateTokenSpeed` (via `updateTokenSpeed` / `updateTokenSpeeds`, and also the keeper path `syncTokenSpeed`) only requires `onlyIfTokenExists(token_)` — i.e., the token is registered in `Pool` — it does **not** require `token_.totalSupply() > 0` before starting emissions: [3](#0-2) 
- Supply can also return to zero *after* emissions start, since `DepositToken.withdraw`/`transfer` and `DebtToken` burns are unrestricted w.r.t. speed.

Additionally, `updateBeforeMintOrBurn` is permissionless ("This function also may be called by anyone"), so any EOA can poke the index during the zero-supply window, advancing `timestamp` and cementing the loss without waiting for a mint/burn: [4](#0-3) 

### Impact Explanation
Every second of emissions that elapses while a tracked token's supply is zero produces `speed` reward tokens that are accounted for (timestamp moved forward) but distributed to no one. Because `rewardToken` is transferred in only via external funding and out only through `_transferRewardIfEnoughTokens`, those tokens remain locked in `RewardsDistributor` forever — permanent freezing of unclaimed yield. The loss is proportional to the zero-supply duration and can cover the entire emission if a speed is set before the first deposit (e.g., a newly added `DepositToken`/`DebtToken` whose rewards are switched on ahead of liquidity, mirroring the reference report's "`setRewards` before any stake" scenario).

### Likelihood Explanation
- Zero-supply states are routine: a token is registered and speed-configured before the first deposit, or all holders withdraw/repay while speed stays non-zero (deployments keep speeds synced to external Vesper reward rates via `syncTokenSpeed`, which can keep speed > 0 independent of Metronome-side supply).
- Any unprivileged user can call `updateBeforeMintOrBurn` during such windows to burn the accrued emissions; no privileged role, oracle, or bridge is needed to realize the loss.
- No modifier (`onlyGovernor`, `onlyIfTokenExists`, `nonReentrant`) prevents the loss; the check that should exist (`totalSupply > 0` gating timestamp advancement) is missing by design.

### Recommendation
In `_calculateTokenIndex`, do not advance `_newTimestamp` when `totalSupply == 0` (or when `_ratio == 0` while `_tokensAccrued > 0`), so emissions resume from the last accrual point once supply exists. Alternatively, gate `_updateTokenSpeed` on `token_.totalSupply() > 0` and/or auto-zero the speed when supply reaches zero, matching the fix in the reference report (ensure at least one holder before emissions start).

### Proof of Concept
Hardhat-style reproduction mirroring `test/RewardDistributor.test.ts` conventions (mocked `msdTOKEN1`):

```ts
// given: speed set, token registered, zero supply
await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, parseEther('1'));
msdTOKEN1.totalSupply.returns(0);
msdTOKEN1.balanceOf.returns(0);

// 10 seconds elapse while supply == 0 → 10 reward tokens emitted
await increaseTimeOfNextBlock(10);

// anyone advances the index; timestamp moves, index doesn't
await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address);
const { index, timestamp } = await rewardDistributor.tokenStates(msdTOKEN1.address);
// index unchanged (still INITIAL_INDEX), timestamp == now
// => 10 reward tokens are now unreachable by any account

// when: alice becomes the only holder afterward
msdTOKEN1.totalSupply.returns(parseEther('100'));
msdTOKEN1.balanceOf.returns(parseEther('100'));
await increaseTimeOfNextBlock(10);
await mine();

// then: she only earns the post-supply 10, never the first 10
const claimable = await rewardDistributor['claimable(address)'](alice.address);
expect(claimable).to.eq(parseEther('10')); // expected 20 if no loss
```

On a fork, the equivalent uses a real `DepositToken` with an active speed: fund `RewardsDistributor` with `rewardToken`, let time pass with `totalSupply == 0`, call `updateBeforeMintOrBurn` from an EOA, deposit, advance time, and observe `claimable` excludes the zero-supply emissions while the tokens remain in the contract.

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

**File:** contracts/RewardsDistributor.sol (L287-304)
```text
    function _updateTokenSpeed(
        IERC20 token_,
        uint256 newSpeed_
    ) private onlyIfDistributorExists onlyIfTokenExists(address(token_)) {
        uint256 _currentSpeed = tokenSpeeds[token_];
        if (_currentSpeed > 0) {
            _updateTokenIndex(token_);
        } else if (newSpeed_ > 0) {
            // Add token to the list
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
            } else {
                // Update timestamp to ensure extra interest is not accrued during the prior period
                tokenStates[token_].timestamp = block.timestamp.toUint32();
            }
        }
```
