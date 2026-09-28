### Title
Off-by-one in `RewardsDistributor._calculateTokenDelta` lets existing holders claim massively inflated rewards — (`File: contracts/RewardsDistributor.sol`)

### Summary
When a reward token's index is exactly `INITIAL_INDEX` (1e18), the fallback that substitutes `INITIAL_INDEX` for a zero `accountIndexOf` does not trigger because the condition uses `>` instead of `>=`. Accounts holding a `DepositToken`/`DebtToken` balance before rewards were enabled keep `accountIndexOf == 0` and are credited `balance * INITIAL_INDEX` in rewards, draining the distributor's reward token balance.

### Finding Description
`_calculateTokenDelta` computes the accrued reward delta as `balance * (tokenIndex - accountIndex)`. To protect users who had no recorded index, it falls back:

```solidity
// contracts/RewardsDistributor.sol:225-230
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

When a token's speed is first set, `_updateTokenSpeed` stores `TokenState({index: INITIAL_INDEX, timestamp: now})`. If an account holds a balance at that moment but has `accountIndexOf == 0` — which is the case for any holder whose mint/transfer happened *before* the speed was set, since `updateBeforeMintOrBurn`/`updateBeforeTransfer` only write the index when `tokenStates[token_].index > 0` — then in the window where `_tokenIndex == INITIAL_INDEX` (before any index accrual), the fallback is skipped (`>` is strict). `_deltaIndex` becomes `INITIAL_INDEX - 0 = 1e18`, and `_tokensDelta = balance * 1e18`.

`_updateTokensAccruedOf` adds this to `tokensAccruedOf[account]`, and `_transferRewardIfEnoughTokens` pays out `tokensAccruedOf` capped only by the contract's reward token balance — so the attacker receives the entire distributor balance.

### Impact Explanation
Direct theft of the reward token inventory held by `RewardsDistributor` — i.e., theft of unclaimed yield owed to all other depositors/borrowers. An attacker holding (or flash-acquiring and holding through the boundary) any balance of a newly reward-enabled token can call `claimRewards(attacker, [token])` before the index accrues past `INITIAL_INDEX` and sweep the full reward balance in one transaction. This is repeatable across every newly added reward token (up to `MAX_REWARD_TOKENS`).

### Likelihood Explanation
High whenever governance enables rewards on a token that already has holders — a routine operational event. The attacker needs only a public `claimRewards` call with a nonzero token balance; no privileged role, no oracle manipulation, and no reentrancy is required. `nonReentrant` does not help because the exploit is a single call. Timing requirement: the claim must land before the supply index grows above `INITIAL_INDEX`, i.e., within the same block or before the next `_updateTokenIndex` accrual — easily achieved by calling `claimRewards` in the same transaction/immediately after `updateTokenSpeed` confirms, or by frontrunning/sandwiching it. Even if missed, `syncTokenSpeed` resets `tokenStates[token_].timestamp` (not the index), so the index monotonically grows — the strict-`>` boundary is only exploitable at exactly `INITIAL_INDEX`, which still leaves the add-token block itself as a reliable window.

### Recommendation
Change the strict comparison to inclusive:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

This matches the intended semantic "if the account has no index, baseline it at the initial index" and eliminates the `deltaIndex = INITIAL_INDEX` edge case.

### Proof of Concept
Hardhat sketch:

```ts
it('drains reward balance via off-by-one at INITIAL_INDEX', async () => {
  // setup: pool, depositToken, rewardsDistributor with rewardToken funded
  await rewardToken.mint(distributor.address, parseEther('1000000'))

  // attacker deposits BEFORE reward speed is set (accountIndexOf stays 0)
  await depositToken.connect(attacker).deposit(parseEther('1'))
  expect(await distributor.accountIndexOf(depositToken.address, attacker.address)).eq(0)

  // governor enables rewards -> tokenStates.index = INITIAL_INDEX
  await distributor.updateTokenSpeed(depositToken.address, parseEther('0.01'))
  expect((await distributor.tokenStates(depositToken.address)).index).eq(parseEther('1'))

  // claim in the same block (index still == INITIAL_INDEX)
  const balBefore = await rewardToken.balanceOf(attacker.address)
  await distributor.claimRewards(attacker.address, [depositToken.address])

  // attacker received tokensAccruedOf = 1 * 1e18 * 1e18-scaled delta => entire balance
  expect(await rewardToken.balanceOf(attacker.address)).eq(
    balBefore.add(parseEther('1000000'))
  )
})
```