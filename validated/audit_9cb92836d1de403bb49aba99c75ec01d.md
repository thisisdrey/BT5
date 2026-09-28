### Title
RewardsDistributor grants `balance * INITIAL_INDEX` rewards on a user's first touch after a reward token is registered - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
A use-after-free analog: `RewardsDistributor._calculateTokenDelta` uses a "freed"/unset `accountIndexOf` slot (value `0`) as if it were a valid index. When a reward token is registered for a DepositToken/DebtToken whose holder has never been indexed, the holder's first accrual computes `deltaIndex = INITIAL_INDEX` instead of `0`, crediting them rewards equal to their entire token balance. Any unprivileged holder can claim this via the permissionless `claimRewards`.

### Finding Description
The bug sits in `_calculateTokenDelta` at contracts/RewardsDistributor.sol:217-231:

```solidity
_tokenIndex = _tokenState.index;
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

The fallback `_accountIndex = INITIAL_INDEX` only triggers when `_tokenIndex > INITIAL_INDEX`. When a token is freshly registered in `_updateTokenSpeed` (contracts/RewardsDistributor.sol:296-299), its state is set to `TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})`. In the same block:

- `_calculateTokenIndex` returns `(0,0)` because `_deltaTimestamps == 0`, so the stored index stays exactly `INITIAL_INDEX`.
- For any holder whose `accountIndexOf` is `0` (balance received before registration and never minted/burned/transferred since — `updateBeforeMintOrBurn`/`updateBeforeTransfer` skip accrual while `tokenStates[token_].index == 0`), the strict `>` check fails, leaving `_accountIndex = 0`.
- `_deltaIndex = INITIAL_INDEX`, so `_tokensDelta = balanceOf(account).wadMul(1e18) = balanceOf(account)` — the holder's full deposit/debt balance is booked as claimable `rewardToken` in `tokensAccruedOf` (line 261-265).

`claimRewards` (lines 141-168) is callable by anyone for any account; `_transferRewardIfEnoughTokens` (lines 248-256) pays out as long as the distributor holds enough `rewardToken`. The same defective delta also flows into the public `claimable` view and `updateBeforeMintOrBurn`/`updateBeforeTransfer` (both permissionless, lines 175-192).

### Impact Explanation
Theft of unclaimed yield: an attacker who is a pre-existing holder of a DepositToken/DebtToken receives `rewardToken` equal to their full token balance (e.g., a whale holding $XM of `msdVaUSDC` claims $XM of MET), draining the RewardsDistributor up to its balance and stealing rewards accrued for all other users. Claiming is fully permissionless — the attacker calls `claimRewards(attacker)` directly, no privileged role needed.

### Likelihood Explanation
Requires one precondition: governor calls `updateTokenSpeed(s)` to enable rewards on a token the attacker already holds with `accountIndexOf == 0`, and the attacker claims in the same block before `block.timestamp` advances the index past `INITIAL_INDEX` (front-running/back-running the governor tx, or via `Operator.execute` bundling). This is a deterministic same-transaction window — analogous to the CVE where a stale freed variable is consumed before reallocation. Note `claimRewards`/`updateBeforeMintOrBurn` are permissionless, so the attacker can time the accrual freely within that block. After the index exceeds `INITIAL_INDEX` the fallback fixes the index, so the window is narrow but reliable.

### Recommendation
Change the zero-index fallback to apply whenever `_accountIndex == 0` (not only when `_tokenIndex > INITIAL_INDEX`), e.g.:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex > INITIAL_INDEX ? INITIAL_INDEX : _tokenIndex;
}
```

or simply `_accountIndex = INITIAL_INDEX` unconditionally when `_accountIndex == 0`, so a never-indexed account accrues `0` delta at registration. Also consider recording `accountIndexOf` at token registration for all current holders is impractical — the fallback fix suffices.

### Proof of Concept
Hardhat fork test sketch (reusing the repo's fixture style, e.g. test/SmartFarmingManager.test.ts setup with `pool`, `msdVaDAI`, `rewardsDistributor`, `rewardToken`):

```ts
// 1. Alice deposits BEFORE rewards are enabled on msdVaDAI
await vaDAI.connect(alice).approve(msdVaDAI.address, MaxUint256)
await msdVaDAI.connect(alice).deposit(parseEther('1000'), alice.address)
// accountIndexOf[msdVaDAI][alice] == 0 (tokenStates.index was 0 at deposit)

// 2. Fund distributor and enable speed in same block context
await rewardToken.mint(rewardsDistributor.address, parseEther('100000'))
// governor registers token (precondition config step)
await rewardsDistributor.connect(governor).updateTokenSpeed(msdVaDAI.address, parseEther('1'))
// mine exactly 0 additional seconds: claim in same block via evm automine off or next tx same timestamp

// 3. Attacker (alice) claims in the same block
await network.provider.send('evm_setAutomine', [false])
// ... or simply claim before time advances on a fork where timestamp delta can be 0
const tx = await rewardsDistributor.connect(alice).claimRewards(alice.address)

// Expected (buggy): tokensAccruedOf[alice] == balanceOf(alice) == ~1000e18
// so alice receives ~1000e18 rewardToken for zero elapsed accrual time
expect(await rewardToken.balanceOf(alice.address)).to.be.closeTo(
  await msdVaDAI.balanceOf(alice.address), parseEther('0.01'))
```

Invariant broken: reward accrual conservation — a user with zero elapsed index accrues a `deltaIndex` of `1e18`, converting their deposit balance into reward claims. Uncertainty note: I could not fully verify within this pass whether `claimRewards` in the same block as `updateTokenSpeed` preserves `block.timestamp` ordering on the deployed config (it does — `_deltaTimestamps` uses `block.timestamp - _supplyState.timestamp`, which is 0 same-block), nor whether deployments have distributors pre-registered for all existing tokens; if every existing token already has a nonzero `accountIndexOf` path covered, the residual risk applies only to newly added DepositToken/DebtToken reward pairs.