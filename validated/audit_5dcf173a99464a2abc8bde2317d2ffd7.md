### Title
`RewardsDistributor` treats a never-indexed account's index as `0` instead of `INITIAL_INDEX` when the token index is still at its initial value, letting a holder claim reward tokens equal to their entire token balance - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
When a reward speed is first enabled for a token, `tokenStates[token_].index` is initialized to `INITIAL_INDEX` (`1e18`) in `_updateTokenSpeed` [1](#0-0) . In `_calculateTokenDelta`, an account whose `accountIndexOf` was never written falls back to `INITIAL_INDEX` **only if** `_tokenIndex > INITIAL_INDEX`; when the index is still exactly `INITIAL_INDEX` the account index remains `0`, so `_deltaIndex = 1e18` and `tokensDelta = balanceOf(account).wadMul(1e18)` — i.e. the account is credited reward tokens equal to its full token balance [2](#0-1) . This mirrors the L2ECO bug class: a per-account multiplier/index is initialized to a stale default (`0` / genesis `INITIAL_INFLATION_MULTIPLIER`) rather than the correct current index, causing massive mis-crediting.

### Finding Description
- `DebtToken` and `DepositToken` call `updateBeforeMintOrBurn`/`updateBeforeTransfer` on each `RewardsDistributor` before balance changes, but those hooks are no-ops while `tokenStates[token_].index == 0` [3](#0-2) [4](#0-3) .
- An unprivileged user can therefore deposit collateral and hold `DepositToken` (or borrow and hold `DebtToken`) before rewards are enabled, leaving `accountIndexOf[token_][attacker] == 0` legitimately.
- When the governor later enables a speed, `tokenStates[token_] = {index: INITIAL_INDEX, timestamp: now}` [5](#0-4) .
- In the same block (before any `deltaTimestamps > 0` growth in `_calculateTokenIndex` [6](#0-5) ), the attacker calls `claimRewards(attacker)` → `tokenStates[token_].index > 0` → `_updateTokenIndex` (no change, same timestamp) → `_updateTokensAccruedOf` → `_calculateTokenDelta` returns `_accountIndex = 0` because `_tokenIndex > INITIAL_INDEX` is false → `_tokensDelta = balance * 1e18 / 1e18 = balance`.
- `_transferRewardIfEnoughTokens` pays out the attacker's `tokensAccruedOf` in the reward token up to the distributor's full balance [7](#0-6) , and `accountIndexOf` is set to `INITIAL_INDEX`, so the drain is one-shot per account but repeatable across attacker-controlled sybil accounts holding any balance.

### Impact Explanation
Direct theft of reward-token funds held by the `RewardsDistributor`: each wei of `DepositToken`/`DebtToken` balance held before speed activation mints a claim for ~1 reward token, capped only by the distributor's reward balance. A large depositor (or many small accounts) can empty accumulated rewards meant for all stakers. This is theft of unclaimed yield / reward funds, reachable purely through public entry points (`deposit`/`issue`, then `claimRewards`).

### Likelihood Explanation
Requires the attacker to hold a tracked balance before `updateTokenSpeed(s)` first activates a token — a predictable, publicly visible protocol event (reward program launches are announced; the `updateTokenSpeeds` tx can also be back-run in the same block since the index growth needs `deltaTimestamps > 0`). No privileged role, oracle manipulation, or malicious endpoint is needed on the attacker's side; the exploit call is the permissionless `claimRewards`. Even without same-block timing, any window where `index` has not yet grown above `INITIAL_INDEX` works.

### Recommendation
In `_calculateTokenDelta`, treat a missing account index as the current index baseline whenever tracking exists, e.g. change the fallback to `_accountIndex == 0 → _accountIndex = INITIAL_INDEX` unconditionally when `_tokenIndex > 0` (or use `>=`), so an untouched account accrues zero delta until its balance changes. Equivalently, the boundary check should be `if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX)`.

### Proof of Concept
```ts
// Hardhat-style, modeled on test/RewardDistributor.test.ts
it('drains rewards for a pre-rewards balance holder', async function () {
  // 1) Attacker holds depositToken BEFORE speed is set; hooks no-op because index == 0
  msdTOKEN1.totalSupply.returns(parseEther('1000'))
  msdTOKEN1.balanceOf.returns(parseEther('100'))   // attacker's balance

  expect(await rewardDistributor.accountIndexOf(msdTOKEN1.address, alice.address)).eq(0)

  // 2) Governor enables rewards: tokenStates[msdTOKEN1] = {index: 1e18, ts: now}
  await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, parseEther('1'))

  // 3) Same block (deltaTimestamps == 0 → index still == INITIAL_INDEX)
  //    Fund distributor and claim
  await vsp.mint(rewardDistributor.address, parseEther('500'))
  const balBefore = await vsp.balanceOf(alice.address)
  await rewardDistributor.claimRewards(alice.address)

  // tokensDelta = 100 tokens * 1e18 / 1e18 = 100 reward tokens stolen
  expect(await vsp.balanceOf(alice.address)).eq(balBefore.add(parseEther('100')))
})
```
Expected outcome: with `tokenStates.index == INITIAL_INDEX` and `accountIndexOf == 0`, `_deltaIndex = 1e18` and the claim pays out `balanceOf(alice)` worth of reward tokens instead of `0`, draining distributor funds up to its balance.

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

**File:** contracts/RewardsDistributor.sol (L186-192)
```text
    function updateBeforeTransfer(IERC20 token_, address from_, address to_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, from_);
            _updateTokensAccruedOf(token_, to_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L197-212)
```text
    function _calculateTokenIndex(
        TokenState memory _supplyState,
        IERC20 token_
    ) private view returns (uint224 _newIndex, uint32 _newTimestamp) {
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
    }
```

**File:** contracts/RewardsDistributor.sol (L217-231)
```text
    function _calculateTokenDelta(
        TokenState memory _tokenState,
        IERC20 token_,
        address account_
    ) private view returns (uint256 _tokenIndex, uint256 _tokensDelta) {
        _tokenIndex = _tokenState.index;
        uint256 _accountIndex = accountIndexOf[token_][account_];

        if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
            _accountIndex = INITIAL_INDEX;
        }

        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
    }
```

**File:** contracts/RewardsDistributor.sol (L248-255)
```text
    function _transferRewardIfEnoughTokens(address account_, uint256 amount_) private {
        IERC20 _rewardToken = rewardToken;
        uint256 _balance = _rewardToken.balanceOf(address(this));
        if (amount_ > 0 && amount_ <= _balance) {
            tokensAccruedOf[account_] = 0;
            _rewardToken.safeTransfer(account_, amount_);
            emit RewardClaimed(account_, amount_);
        }
```

**File:** contracts/RewardsDistributor.sol (L294-299)
```text
        } else if (newSpeed_ > 0) {
            // Add token to the list
            if (tokenStates[token_].index == 0) {
                if (tokens.length == MAX_REWARD_TOKENS) revert ReachedMaxRewardTokens();
                tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp.toUint32()});
                tokens.push(token_);
```
