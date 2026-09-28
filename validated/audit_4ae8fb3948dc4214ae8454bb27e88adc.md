### Title
`RewardsDistributor._calculateTokenDelta` fails to classify the `INITIAL_INDEX` state as "no accrual", letting an attacker accrue their full token balance as rewards in the same block a speed is first set - (File: contracts/RewardsDistributor.sol)

### Summary
The kernel bug fixed by CVE-2023-53361 was a missing `pmd_leaf()` definition: a predicate that should classify a page-table entry as a leaf was absent, so a huge page was misclassified and a downstream invariant broke (panic on `pte_present`). The analog in Metronome is the boundary predicate in `RewardsDistributor._calculateTokenDelta`: the `_accountIndex == 0` fallback only initializes the account index when `_tokenIndex > INITIAL_INDEX`, but not when `_tokenIndex == INITIAL_INDEX` — the exact state the index is in during the timestamp a reward speed is first enabled. In that window the missing classification produces a full `_deltaIndex == INITIAL_INDEX`, i.e. the account is credited `balance * 1` reward tokens instantly.

### Finding Description
`tokenStates[token_]` is initialized lazily in `_updateTokenSpeed` when a token's speed first becomes non-zero: `tokenStates[token_] = TokenState({index: INITIAL_INDEX, timestamp: block.timestamp})` (`RewardsDistributor.sol:296-299`).

`_calculateTokenDelta` handles the "account has no recorded index" case with:

```solidity
if (_accountIndex == 0 && _tokenIndex > INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
uint256 _deltaIndex = _tokenIndex - _accountIndex;
_tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

(`contracts/RewardsDistributor.sol:225-230`)

Because the guard uses strict `>` rather than `>=`, when the global index is still exactly `INITIAL_INDEX` (i.e. `block.timestamp == tokenStates.timestamp`, the same block the speed was set — `_calculateTokenIndex` returns no update when `_deltaTimestamps == 0`, lines 203-211), an account with `_accountIndex == 0` gets `_deltaIndex = INITIAL_INDEX - 0 = 1e18`, and `_tokensDelta = balanceOf(account).wadMul(1e18) = balanceOf(account)`.

Both `updateBeforeMintOrBurn`, `updateBeforeTransfer` (permissionless, lines 175-192) and `claimRewards` (line 150) reach this code whenever `tokenStates[token_].index > 0`, which is true immediately after the governor's `updateTokenSpeed` tx executes.

### Impact Explanation
Attacker pre-deposits collateral (or holds debt-token balance) in a rewarded DepositToken/DebtToken before rewards are enabled. They watch the mempool for the governor's `updateTokenSpeed`/`syncTokenSpeed` transaction for that token and, in the same block (same `block.timestamp`), call `claimRewards(attacker)`. `tokensAccruedOf[attacker]` is credited their full deposit-token balance denominated in reward tokens, and `_transferRewardIfEnoughTokens` (line 248) transfers it as long as the distributor holds enough `rewardToken`. This is direct theft of unclaimed yield intended for all distributors' users, bounded only by the distributor contract's reward balance and the attacker's deposited balance.

### Likelihood Explanation
Requires a reward token's speed to transition from 0 to non-zero (new incentive launch, or re-enabling a paused token) — a routine governance operation. The attacker only needs an unprivileged EOA, a prior deposit (doable any time in advance, since `updateBeforeMintOrBurn` is a no-op while `index == 0`), and same-block ordering after the governor tx, which is standard front-running. No privileged role, oracle manipulation, or malicious endpoint is needed.

### Recommendation
Change the fallback condition to cover the boundary:

```solidity
if (_accountIndex == 0 && _tokenIndex >= INITIAL_INDEX) {
    _accountIndex = INITIAL_INDEX;
}
```

Or equivalently `if (_accountIndex == 0) _accountIndex = _tokenIndex < INITIAL_INDEX ? _tokenIndex : INITIAL_INDEX`. This treats "index never recorded" accounts as having entered at `INITIAL_INDEX` whenever the global index hasn't yet grown past it — the missing `pmd_leaf`-style predicate case.

### Proof of Concept
Hardhat/Foundry fork sketch:

1. Fork a deployed chain (e.g. mainnet); take a `RewardsDistributor` with `rewardToken` funded.
2. As `alice` (unprivileged), call `depositToken.deposit(X, alice)` — accrues nothing, `tokenStates.index == 0`.
3. Impersonate/schedule governor tx `distributor.updateTokenSpeed(depositToken, speed)`; in the same block and timestamp, as `alice` call `distributor.claimRewards(alice)`.
4. `_updateTokenIndex` is a no-op (`_deltaTimestamps == 0`), `_calculateTokenDelta` yields `_tokensDelta == balanceOf(alice)`, and `rewardToken` is transferred to `alice` up to the distributor's balance.

Expected: `alice`'s reward balance increases by ~`depositToken.balanceOf(alice)` (capped at the distributor's `rewardToken` balance) despite zero elapsed accrual time.