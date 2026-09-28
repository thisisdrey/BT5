One more check on the strongest candidate — the rewards index fallback — plus a quick look at DepositToken for flag-style checks.### Title
RewardsDistributor accrues `balance * 1e18` reward when the global index is still `INITIAL_INDEX` and the account index was never set - (contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenDelta` contains a fallback that treats a never-initialized `accountIndexOf` as `INITIAL_INDEX`, but only when `_tokenIndex > INITIAL_INDEX`. When the global index is exactly `INITIAL_INDEX` — which persists as long as the per-call accrual ratio rounds to zero — the delta is computed against `0`, producing `tokensDelta = balanceOf(account).wadMul(1e18)`, i.e. a reward equal to the holder's full token balance. Any pre-existing holder (whose `accountIndexOf` stayed `0` because the accrual hooks are no-ops while `index == 0`) can permissionlessly claim this inflated amount and drain the distributor's `rewardToken`.

This is the same bug class as CVE-2021-47300: a state flag/index that is expected to be set is never set (`accountIndexOf == 0`), so the defensive fallback fails to engage exactly on the boundary (`> INITIAL_INDEX` instead of `>=`), and the downstream computation operates on corrupt state.

### Finding Description
In `contracts/RewardsDistributor.sol`:

1. `updateBeforeMintOrBurn` and `updateBeforeTransfer` (lines 175-192) skip all accounting while `tokenStates[token_].index == 0`. The index only becomes non-zero when the governor/keeper sets a positive speed via `_updateTokenSpeed` (line 298), which initializes `index = INITIAL_INDEX` (`1e18`). Therefore every account holding a DepositToken/DebtToken balance *before* the token is added to the distributor has `accountIndexOf[token][account] == 0`.

2. `_calculateTokenIndex` (lines 197-212) returns `_newIndex = _supplyState.index + _ratio` where `_ratio = _tokensAccrued.wadDiv(_totalSupply)`. `wadDiv` truncates, so whenever `speed * elapsed * 1e18 < totalSupply`, `_ratio == 0` and the global index remains exactly `INITIAL_INDEX`. This is trivially achievable with a small speed and a large supply, and it can persist across many calls because the timestamp is updated even when the index does not move (lines 274-280).

3. `_calculateTokenDelta` (lines 217-231):
```solidity
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```
When `_tokenIndex == INITIAL_INDEX` and `_accountIndex == 0`, the fallback does not fire (strict `>`), so `_deltaIndex = 1e18` and `_tokensDelta = balanceOf(account_)` — the accrual intended to represent `balance * (index - INITIAL_INDEX)` instead returns `balance * 1e18`.

4. `claimRewards` (lines 141-168) is permissionless: anyone can call `claimRewards(account, tokens)` for their own account, `_updateTokensAccruedOf` writes the inflated delta into `tokensAccruedOf`, and `_transferRewardIfEnoughTokens` (lines 248-256) pays out `rewardToken` up to the contract's whole balance. No pause flag, health check, `SynthContext` sender check, or reentrancy path prevents this — `nonReentrant` does not apply since no reentrancy is needed.

### Impact Explanation
An attacker who holds a DepositToken (e.g. `msdMET`) or DebtToken position opened before the token's reward speed was set can claim `rewardToken` equal to their token balance — far exceeding any legitimately accrued reward — draining the distributor's reward balance (theft of unclaimed yield belonging to all other users). The attack is permissionless, requires only an unprivileged EOA, and each eligible account (one claim each, since `accountIndexOf` is set to `INITIAL_INDEX` afterward) repeats the theft until the distributor is empty. The broken invariant is reward accrual correctness.

### Likelihood Explanation
The preconditions are realistic and on the normal operational path: (a) DepositTokens gain holders before their reward speed is configured, which is the standard lifecycle — rewards are added to already-live collaterals, and `syncTokenSpeed` keeps speeds synced to Vesper's `PoolRewards` so speeds routinely start small; (b) `wadDiv` truncation makes `_ratio == 0` whenever `speed * elapsed < totalSupply / 1e18`, which holds whenever the configured per-second emission is small relative to supply — a common configuration. No privileged or malicious actor is required; the attacker merely deposits early and calls `claimRewards` once. Likelihood is bounded mainly by whether deployed pools actually run `RewardsDistributor` with funded `rewardToken`.

### Recommendation
Change the fallback condition in `_calculateTokenDelta` to cover the boundary, i.e. treat a missing account index as the current index (not `INITIAL_INDEX`), or at minimum use `>=`:

```solidity
if (_accountIndex == 0) {
    _accountIndex = _tokenIndex; // baseline at current index, delta = 0 on first touch
}
```

Baselining to `_tokenIndex` (rather than `INITIAL_INDEX`) also prevents late-joiners from claiming rewards accrued before they held the token. Alternatively, record `accountIndexOf` unconditionally in `updateBeforeMintOrBurn`/`updateBeforeTransfer` even when `index == 0`, so the "never set" state cannot exist after the first interaction. Add a regression test: deposit before `updateTokenSpeed`, set a small speed, advance time, and assert `claimable(account)` does not exceed `balance * (index - INITIAL_INDEX)`.

### Proof of Concept
Hardhat test in the repo's existing style (`test/` uses ethers + `parseEther`, `time.increase`):

```typescript
it('pays rewardToken equal to deposit balance when index == INITIAL_INDEX', async function () {
  // given: alice deposits BEFORE the reward speed is set -> accountIndexOf stays 0
  await met.connect(alice).approve(msdMET.address, parseEther('1,000'))
  await msdMET.connect(alice).deposit(parseEther('1,000'), alice.address)
  const aliceBal = await msdMET.balanceOf(alice.address) // 1,000e18
  expect(await rewardsDistributor.callStatic.accountIndexOf(msdMET.address, alice.address)).eq(0)

  // fund distributor and set a tiny speed so ratio truncates to 0:
  // speed * elapsed * 1e18 < totalSupply  ->  global index stays INITIAL_INDEX
  await rewardToken.mint(rewardsDistributor.address, parseEther('10,000'))
  await rewardsDistributor.connect(governor).updateTokenSpeed(msdMET.address, 1) // 1 wei/sec

  await time.increase(3600) // index still 1e18: 1 * 3600 * 1e18 / 1e21 == 0

  // when: alice claims
  const before = await rewardToken.balanceOf(alice.address)
  await rewardsDistributor.connect(alice).claimRewards(alice.address, [msdMET.address])

  // then: alice received rewardToken == her full msdMET balance instead of ~0
  expect(await rewardToken.balanceOf(alice.address)).eq(before.add(aliceBal))
})
```

Key trace: `claimRewards` → `tokenStates[msdMET].index > 0` (== `INITIAL_INDEX`) → `_updateTokenIndex` keeps index at `1e18` (ratio rounds to 0) → `_updateTokensAccruedOf` → `_calculateTokenDelta`: `_accountIndex == 0`, `_tokenIndex == INITIAL_INDEX` (not `> INITIAL_INDEX`) → `_deltaIndex = 1e18` → `_tokensDelta = aliceBal` → `_transferRewardIfEnoughTokens` pays `aliceBal` of `rewardToken`.

Uncertainty: exploitability in production depends on deployed `speed`/`totalSupply` magnitudes and whether the `rewardToken` balance in the distributor is non-trivial; the truncation window (`ratio == 0`) must hold at the moment of the claim, which is a configuration/timing condition rather than a guaranteed state.