### Title
Reward tokens lost to index rounding are permanently locked with no recovery function - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor` accrues rewards through a per-token index computed with `wadDiv`/`wadMul`, both of which round down. The remainder of each accrual (tokens emitted by `tokenSpeeds` but never credited to any account) accumulates in the contract's `rewardToken` balance. The contract exposes no sweep/recovery function (it does not inherit `TokenHolder`), so this residue is permanently unrecoverable. The same applies to any `rewardToken` balance left over after `syncTokenSpeed` lowers a speed, or any tokens donated/sent to the contract.

### Finding Description
In `_calculateTokenIndex`, the global index delta is computed as:

```solidity
// contracts/RewardsDistributor.sol:203-207
uint256 _tokensAccrued = _deltaTimestamps * _speed;
uint256 _ratio = _totalSupply > 0 ? _tokensAccrued.wadDiv(_totalSupply) : 0;
_newIndex = (_supplyState.index + _ratio).toUint224();
```

`wadDiv` truncates, so `_ratio * _totalSupply <= _tokensAccrued`, with the difference lost. Per-account accrual in `_calculateTokenDelta` truncates again:

```solidity
// contracts/RewardsDistributor.sol:229-230
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

Claims pay out only `tokensAccruedOf[account]` via `_transferRewardIfEnoughTokens`, which performs a `safeTransfer` capped at the contract balance and silently does nothing when `amount_ > _balance`. The full external surface of the contract (lines 103-355) contains only `claimRewards`, index update hooks, `updateTokenSpeed(s)`, `syncTokenSpeed`, and `updateTokenSpeedKeeper` — no `sweep`/`recover`/`skim` exists, and `RewardsDistributor` does not inherit `TokenHolder` (whose `sweepERC20` exists for other contracts such as gateways).

Two consequences:
1. The rounding remainder of every `_tokensAccrued` emission is never claimable by anyone — a small amount of `rewardToken` per update period is permanently locked.
2. Because `_updateTokenIndex` can be triggered by any unprivileged call (`updateBeforeMintOrBurn`, `updateBeforeTransfer`, `claimRewards`) and `_ratio` truncates, the aggregate claimable is strictly less than `speed * elapsed`, so excess funded rewards (including the entire emission during periods when `_totalSupply == 0`, where `_ratio` is set to 0 and the emission is silently burned in accounting terms) have no exit path.

### Impact Explanation
The contract is upgradeable, so in principle governance could add a sweep later, but on the deployed code there is no function to recover the residue. Funds that were emitted (or funded) for distribution but lost to truncation, or emitted while a tracked token had zero supply, are permanently frozen in the contract — matching the reported bug class of "precision-loss rewards with no recovery path". Individual amounts are dust-scale per period but accumulate over the lifetime of the distributor and across up to 20 tracked tokens.

### Likelihood Explanation
Certain to occur in normal operation: every index update truncates, and any period where `totalSupply == 0` while `speed > 0` permanently forfeits that period's emission. No privileged action or attack is required — unprivileged calls to `updateBeforeMintOrBurn`/`claimRewards` continuously produce the truncation. The magnitude per period is small (sub-wei-of-share rounding), so the aggregate value depends on emission size and duration.

### Recommendation
Add an `onlyGovernor` sweep function that transfers excess `rewardToken` (e.g., `balance - totalPendingAccruals`) to a configurable recipient, and/or account for emissions during zero-supply periods by carrying them forward rather than discarding them (currently `_ratio` is set to `0` at line 206, silently dropping `_deltaTimestamps * _speed`).

### Proof of Concept
Hardhat-style test (mirroring `test/RewardDistributor.test.ts`):

```ts
it('locks rounding dust with no recovery path', async function () {
  // governor sets speed on a deposit token
  await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, parseEther('1'))

  // supply that does not divide the emission cleanly
  msdTOKEN1.totalSupply.returns(BigNumber.from('3'))
  msdTOKEN1.balanceOf.returns(BigNumber.from('3'))

  // fund the distributor with more than any user can claim
  await rewardToken.mint(rewardDistributor.address, parseEther('100'))

  await increaseTimeOfNextBlock(10)
  await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)

  // emitted = 10e18; index delta = floor(10e18 * 1e18 / 3) -> claimable < 10e18
  const accrued = await rewardDistributor.tokensAccruedOf(alice.address)
  expect(accrued).to.be.lt(parseEther('10'))

  await rewardDistributor.claimRewards(alice.address)
  const leftover = await rewardToken.balanceOf(rewardDistributor.address)
  expect(leftover).to.be.gt(parseEther('90')) // includes rounding remainder

  // No external function can move `leftover`: the contract exposes
  // only claimRewards/update*/sync* entry points and inherits no TokenHolder.
})
```

A zero-supply variant is stronger: with `totalSupply = 0` and `speed > 0`, `_ratio = 0`, so that period's entire emission is never credited to anyone and joins the unrecoverable balance.