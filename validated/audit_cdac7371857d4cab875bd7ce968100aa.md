### Title
Zero `accountIndexOf` falls back to 0 (not `INITIAL_INDEX`) when global index equals `INITIAL_INDEX`, crediting the attacker reward tokens equal to their full token balance - (contracts/RewardsDistributor.sol)

### Summary
The XFS bug is an "absent metadata → undersized rounding granularity" bug: when the superblock's log stripe unit is 0, XFS falls back to `l_iclog_roundoff = 512` instead of the real 4096 sector size, corrupting the log. The Metronome analog is in `RewardsDistributor._calculateTokenDelta`: when an account has never been indexed (`accountIndexOf[token][account] == 0`), the fallback to `INITIAL_INDEX` is only applied `if (_tokenIndex > INITIAL_INDEX)`. While the global index is still exactly `INITIAL_INDEX` (speed set, but no time elapsed), the account index stays `0`, so `_deltaIndex = INITIAL_INDEX` and the account is credited `balance * 1` reward tokens.

### Finding Description [1](#0-0) 

`_calculateTokenDelta` handles a missing account index like this:

```solidity
uint256 _accountIndex = accountIndexOf[token_][account_];
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When a token's reward speed is first enabled, `_updateTokenSpeed` stores `tokenStates[token_] = {index: INITIAL_INDEX, timestamp: now}` (line 298). The global index only rises above `INITIAL_INDEX` after `_calculateTokenIndex` observes `_deltaTimestamps > 0` with `_speed > 0` (lines 201–208). In the same block in which the speed is set, `tokenStates[token_].index == INITIAL_INDEX` exactly.

Any account that acquired a balance while rewards were disabled (index == 0, so `updateBeforeMintOrBurn`/`updateBeforeTransfer`/`claimRewards` all skip updates via the `tokenStates[token_].index > 0` guard, lines 156/176/187) has `accountIndexOf == 0`. If such an account triggers `_updateTokensAccruedOf` while the global index is still `INITIAL_INDEX`, the `_tokenIndex > INITIAL_INDEX` guard is false, `_accountIndex` remains 0, `_deltaIndex = 1e18`, and `_tokensDelta = balanceOf(account).wadMul(1e18) = balanceOf(account)`. That amount is added to `tokensAccruedOf[account]` (line 264), and `claimRewards` will pay it out in `rewardToken` via `_transferRewardIfEnoughTokens` (lines 248–256).

The trigger is permissionless: `updateBeforeMintOrBurn(token_, account_)` "may be called by anyone to update stored indexes" (line 173–175). So an attacker does not even need to move tokens — they just call it for their own account in the same block the governor enables the speed (e.g., via a same-block bundle or by racing the `syncTokenSpeed` keeper call that also lands in `_updateTokenSpeed`).

### Impact Explanation
Direct theft of unclaimed yield / reward-token drainage. The attacker is credited `rewardToken` equal to their deposit/debt token balance (denominated in the tracked token's units) without any accrual time passing. They can then call `claimRewards` to pull that amount out of the distributor, stealing rewards meant for other users or draining the distributor's reward-token balance up to their (possibly flash-loan-inflated) balance. The fix's correct value — `INITIAL_INDEX` — is only applied when `_tokenIndex > INITIAL_INDEX`, so the missing-metadata fallback granularity is literally off by the entire base index.

### Likelihood Explanation
Requires: (a) the attacker holds a balance of a deposit/debt token *before* its reward speed is set (or while speed is 0 and index is reset), and (b) execution in the same block as the speed update (or before one second elapses). Both are achievable: holding tokens is free, and `updateBeforeMintOrBurn` is callable by anyone, so the attacker can bundle their call immediately after the governor/keeper transaction via private orderflow. No privileged role, oracle manipulation, or reentrancy is needed; `claimRewards` is `nonReentrant` but that doesn't matter since there is no callback. The payout is capped by the distributor's reward-token balance (`_transferRewardIfEnoughTokens` silently skips if insufficient), and by `MAX_TOKENS_PER_USER`-style nothing — the only limit is the attacker's balance, which can be inflated with a flash loan + same-block deposit… note deposit itself calls `updateBeforeMintOrBurn` *before* minting, so the balance used is the pre-deposit balance; the attacker must already hold the tokens prior to the enabling block (e.g., deposit earlier, then enable-watch).

### Recommendation
Apply the `INITIAL_INDEX` fallback whenever the account index is unset, regardless of the global index value:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex > INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex;
}
```

or equivalently `_accountIndex = Math.max(INITIAL_INDEX, accountIndexOf[...])` guarded so that an unset index never produces a positive delta. Alternatively, in `_updateTokensAccruedOf`, skip crediting when `accountIndexOf` is 0 and the index equals `INITIAL_INDEX`. The invariant to enforce: a never-indexed account's baseline must always equal the current global index, never a hardcoded smaller granularity — exactly the XFS fix ("use the real sector size, not 512").

### Proof of Concept
Hardhat sketch (repo's own test scaffolding, e.g. `test/RewardsDistributor.test.ts` fixture style):

```ts
// 1. Rewards off: tokenStates[msdMET].index == 0, all updates skipped
await met.mint(attacker.address, parseEther('1000'))
await met.connect(attacker).approve(msdMET.address, MaxUint256)
await msdMET.connect(attacker).deposit(parseEther('1000'), attacker.address)
// attacker.accountIndexOf[msdMET] is still 0

// 2. Fund distributor with reward token
await rewardToken.mint(distributor.address, parseEther('100000'))

// 3. Governor enables speed in tx1; attacker bundles tx2 in the SAME block
await distributor.connect(governor).updateTokenSpeed(msdMET.address, parseEther('1'))
await network.provider.send('evm_setAutomine', [false]) // or use same-block bundle
//    tokenStates[msdMET].index == INITIAL_INDEX (1e18), timestamp == now

// 4. Same block: permissionless index poke
await distributor.connect(attacker).updateBeforeMintOrBurn(msdMET.address, attacker.address)
//    _tokenIndex == INITIAL_INDEX -> guard fails -> deltaIndex = 1e18
//    tokensAccruedOf[attacker] = 1000e18

// 5. Mine next block and claim
await distributor.connect(attacker).claimRewards(attacker.address)
expect(await rewardToken.balanceOf(attacker.address)).to.eq(parseEther('1000'))
```

The exploit fails (correct behavior) only if a block boundary passes between steps 3 and 4, letting `_tokenIndex` exceed `INITIAL_INDEX` — confirming the bug is precisely the missing `INITIAL_INDEX` fallback when the stored index equals the base value.

### Citations

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
