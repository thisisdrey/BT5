### Title
RewardsDistributor accrues a full-balance reward when token index equals `INITIAL_INDEX` - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
The advisory's bug class is an accounting/coverage gap: certain state transitions are not fully captured by the monitoring logic, letting some events slip through. The Metronome analog is in `RewardsDistributor._calculateTokenDelta`: when a reward token's supply index is still exactly `INITIAL_INDEX` (i.e., the index was initialized but has not yet advanced), an account with `accountIndexOf == 0` is not rebased to `INITIAL_INDEX`. The computed delta becomes `INITIAL_INDEX` itself, so `tokensDelta = balance * 1` — the account is credited rewards equal to its entire DepositToken/DebtToken balance even though zero reward time elapsed.

### Finding Description
`_calculateTokenDelta` handles the "account never seen" case only when the global index has already moved past the initial value:

```solidity
// contracts/RewardsDistributor.sol:223-230
uint256 _accountIndex = accountIndexOf[token_][account_];

if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}

uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When a token speed is first activated, `_updateTokenSpeed` writes `tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: now})` (lines 296-299). Until at least one second passes and `_updateTokenIndex` runs, `index == INITIAL_INDEX`. In that window, for any account with `accountIndexOf == 0`:

- `_tokenIndex > INITIAL_INDEX` is `false`, so `_accountIndex` stays `0`
- `_deltaIndex = 1e18 - 0 = 1e18`
- `_tokensDelta = balance.wadMul(1e18) = balance`

`_updateTokensAccruedOf` then writes `tokensAccruedOf[account] += balance` (lines 261-265). This update is reachable from `updateBeforeTransfer`/`updateBeforeMintOrBurn` (called on every DepositToken `transfer`, `transferFrom`, `seize`, `withdraw`, `deposit`) or directly via `claimRewards`, which is permissionless and takes an arbitrary account list.

The same off-by-one boundary also means that when `_tokenIndex` later returns to a comparison, an account first touched inside this window is permanently recorded with `accountIndexOf = INITIAL_INDEX` plus a bogus accrued amount.

### Impact Explanation
Direct theft of unclaimed yield (accepted impact class). In the activation block an attacker who already holds msdTOKEN (or debt tokens, if the debt token is rewarded) calls `claimRewards([attacker], [token])`, gets `tokensAccruedOf[attacker] = attackerBalance`, and `_transferRewardIfEnoughTokens` pays out `rewardToken` up to the distributor's full balance. With a large pre-positioned deposit (including same-block flash-funded collateral via `NativeTokenGateway`/`VesperGateway` and `Pool.swap`), the accrued amount can exceed the distributor's reward balance, draining all undistributed rewards. Rewards belonging to legitimate users are stolen.

### Likelihood Explanation
The window is narrow: it requires `_updateTokensAccruedOf` to execute while `tokenStates[token_].index == INITIAL_INDEX`, i.e., in the same block/timestamp in which `updateTokenSpeed`, `updateTokenSpeeds`, or `syncTokenSpeed` first activates a token. `syncTokenSpeed` is callable by the `tokenSpeedKeeper` and `updateTokenSpeeds` by governor — the attacker does not need a privileged role, only to pre-hold a balance and land a `claimRewards`/`transfer` in the activation block (front-run or same-block back-run of the keeper transaction). Because `tokenSpeedKeeper` syncs speeds externally (line 333), activation transactions are public and predictable. Severity is bounded by reward-token balance and the feasibility of same-block ordering on the deployment chain (straightforward on L2s like Optimism/Base with public mempools/ordering, or when the keeper tx is observed).

### Recommendation
Fix the boundary condition in `_calculateTokenDelta` to rebase unseen accounts whenever the index is initialized:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

Alternatively, have `_updateTokenSpeed` initialize `accountIndexOf` semantics such that `tokensDelta` is zero while the index has not advanced, and add a regression test covering `claimRewards` invoked in the same block as `updateTokenSpeed`/`syncTokenSpeed` activation.

### Proof of Concept
Hardhat sketch (fork or local deployment with a RewardsDistributor whose reward token is funded):

```ts
// setup: alice deposits collateral -> holds msdTOKEN balance B
await msdUSDC.deposit(parseUnits('10000', 6), alice.address)
const B = await msdUSDC.balanceOf(alice.address)

// keeper/governor activates speed for msdUSDC; index = INITIAL_INDEX (1e18), timestamp = now
await rewardsDistributor.connect(keeper).syncTokenSpeed(msdUSDC.address)
// or: await rewardsDistributor.connect(governor).updateTokenSpeed(msdUSDC.address, speed)

// same block (automine off / same timestamp): alice claims
await network.provider.send('evm_setAutomine', [false])
await rewardsDistributor.claimRewards(alice.address, [msdUSDC.address])
await network.provider.send('evm_mine')

// tokensAccruedOf[alice] was set to B * 1e18/1e18 = B despite zero elapsed reward time
// _transferRewardIfEnoughTokens pays min(B, rewardToken.balanceOf(distributor)) to alice
const paid = await rewardToken.balanceOf(alice.address)
expect(paid).to.be.gt(0) // proportional to deposit balance, not to time*speed
```

Key assertion: `tokensAccruedOf[alice]` jumps to `balanceOf(alice)` while `tokenStates[msdUSDC].index == INITIAL_INDEX` and `block.timestamp == tokenStates.timestamp`, demonstrating rewards were credited for a period that never elapsed.

Caveat: I could not execute the PoC in this environment, and the exploit window depends on a token being activated (or re-activated after `index` was reset) while an attacker holds a balance — on deployments where all rewarded tokens already have `index > INITIAL_INDEX`, the flawed branch is unreachable until a new token speed is set.