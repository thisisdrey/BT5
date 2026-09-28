### Title
Accounts first updated while a token's global index is still `INITIAL_INDEX` are credited reward tokens equal to their full token balance - ([File: contracts/RewardsDistributor.sol](contracts/RewardsDistributor.sol))

### Summary
`RewardsDistributor._calculateTokenDelta` only applies the `accountIndex == 0 -> INITIAL_INDEX` fallback when the global index is strictly greater than `INITIAL_INDEX`. While the index still equals `INITIAL_INDEX` (the same block/timestamp in which the governor enables a non-zero speed via `updateTokenSpeed`/`syncTokenSpeed`), a first-time accrual computes `_deltaIndex = INITIAL_INDEX - 0 = 1e18`, so `_tokensDelta = balance.wadMul(1e18) = balance`. Any unprivileged account can trigger accrual for itself via the permissionless `updateBeforeMintOrBurn` or `claimRewards`, instantly crediting `tokensAccruedOf[account]` with an amount equal to its entire DepositToken/DebtToken balance and draining the distributor's `rewardToken`.

### Finding Description
Relevant code in `contracts/RewardsDistributor.sol`:

```solidity
// L217-231
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

When a token's speed is enabled, `_updateTokenSpeed` stores `tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` (L296-299). Until `block.timestamp` advances past that timestamp, `_calculateTokenIndex` returns `(0, 0)` and the index remains `INITIAL_INDEX`. During that window, any account with `accountIndexOf[token_][account] == 0` gets `_deltaIndex = 1e18` instead of 0, i.e. is credited `balanceOf(account)` reward units rather than the correct `0`.

Attack path (all public entry points, attacker unprivileged):

1. Governor calls `updateTokenSpeed(depositToken, speed)` (or keeper calls `syncTokenSpeed`) in block `T`.
2. In the same block, the attacker calls `pool.deposit(...)` (or already holds the deposit/debt token). The pre-mint hook `updateRewardsBeforeMintOrBurn` accrues with balance 0, then the attacker holds `B > 0` tokens.
3. Still in block `T`, attacker calls `rewardsDistributor.updateBeforeMintOrBurn(depositToken, attacker)` — this is explicitly callable by anyone (L173-175). Index is still `INITIAL_INDEX`, so `_tokensDelta = B`.
4. Attacker calls `claimRewards(attacker)` → `_transferRewardIfEnoughTokens` transfers `B` units of `rewardToken` (L248-256).

The analogous invariant to the Sushi report's broken claim path holds here in the opposite direction: the accrual math in the reward-claim pipeline is wrong for a reachable state, so rewards are paid out far in excess of entitlement.

### Impact Explanation
Theft of unclaimed yield: the attacker receives `rewardToken` equal to their deposit/debt token balance without any time having elapsed and without having accrued anything. The credited `tokensAccruedOf` is settled against the distributor's real `rewardToken` balance, draining funds earmarked for all legitimate users. With a large deposit (or a flash-loan-funded deposit, since deposit→accrue→claim can all happen atomically in the same block) the attacker can extract essentially the distributor's whole reward balance in one transaction.

### Likelihood Explanation
- Requires only that an attacker hold or mint a balance of a token in the same block its speed is first set (or re-set after `index` was never initialized — note re-enabling uses the `else` branch at L300-303 and is not affected, so the window is the first activation per token). Each new DepositToken/DebtToken reward activation on mainnet, optimism, base, hemi, etc. reopens the window.
- `updateBeforeMintOrBurn` and `claimRewards` are permissionless; no modifier (`onlyIfTokenExists`, `nonReentrant`, pause) blocks the path. `nonReentrant` does not matter since deposit and claim are separate calls.
- On L2s and via builder/backrun bundles on mainnet, landing a transaction in the same block as the governor's `updateTokenSpeed` is feasible and, on L2s with public mempools/sequencer ordering, straightforward to attempt repeatedly.

### Recommendation
Fix the fallback in `_calculateTokenDelta` so a never-set account index is treated as the current global index whenever the account has no recorded index for a token whose index has already been initialized:

```solidity
if (_accountIndex == 0 && _tokenIndex > 0) {
    _accountIndex = _tokenIndex; // or INITIAL_INDEX when _tokenIndex == INITIAL_INDEX
}
```

Equivalently, set `_accountIndex = INITIAL_INDEX` whenever `accountIndexOf == 0` and `_tokenIndex >= INITIAL_INDEX`. Optionally also record `accountIndexOf` at speed-activation time, or only allow `_updateTokensAccruedOf` to run once the index has moved past `INITIAL_INDEX`.

### Proof of Concept
Hardhat test (style matches `test/RewardDistributor.test.ts`):

```ts
it('credits full balance as rewards when index is still INITIAL_INDEX', async function () {
  // rewardToken = vsp, distributor funded
  await vsp.mint(rewardDistributor.address, parseEther('1000'))

  // attacker already holds a balance of the deposit token
  const attackerBalance = parseEther('100')
  msdTOKEN1.totalSupply.returns(attackerBalance)
  msdTOKEN1.balanceOf.whenCalledWith(alice.address).returns(attackerBalance)

  // governor enables speed -> tokenStates[msdTOKEN1].index = INITIAL_INDEX, timestamp = now
  await rewardDistributor.updateTokenSpeed(msdTOKEN1.address, parseEther('1'))

  // same timestamp: attacker accrues via the permissionless hook
  await rewardDistributor.updateBeforeMintOrBurn(msdTOKEN1.address, alice.address)

  // BUG: alice was credited her full balance, not 0
  const accrued = await rewardDistributor.tokensAccruedOf(alice.address)
  expect(accrued).to.eq(attackerBalance) // should be 0

  // and she can claim it immediately, draining the distributor
  await rewardDistributor['claimRewards(address)'](alice.address)
  expect(await vsp.balanceOf(alice.address)).to.eq(attackerBalance)
})
```

The bug reproduces whenever `updateBeforeMintOrBurn` / `updateBeforeTransfer` / `claimRewards` runs for a fresh account while `tokenStates[token].index == INITIAL_INDEX` (i.e. same `block.timestamp` as `updateTokenSpeed`), confirmed by the L225 condition failing to trigger and `_deltaIndex = 1e18` at L229-230.