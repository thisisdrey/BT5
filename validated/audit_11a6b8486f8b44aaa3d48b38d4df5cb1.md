### Title
Attacker fills victim's token slots via dust `DepositToken` transfers, permanently reverting deposits/mints (`UserReachedMaxTokens`) — (File: contracts/Pool.sol)

### Summary
`Pool` tracks each account's collateral and debt positions in two `MappedEnumerableSet` sets capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79`). Any transfer of a `DepositToken` to an address whose balance was zero calls `pool.addToDepositTokensOfAccount(recipient)`, which reverts with `UserReachedMaxTokens` once the combined deposit+debt token count reaches 30 (`contracts/Pool.sol:143-148`, `contracts/DepositToken.sol:517-520`). An unprivileged attacker can cheaply obtain dust balances of every listed deposit token and transfer 1 wei of each to a victim, permanently DoS-ing that victim's ability to receive new collateral types — including via `deposit` — until the victim spends gas to clear slots. This mirrors CVE-2016-10067: attacker-controlled accumulation of items into a bounded per-account buffer that overflows into a crash/revert.

### Finding Description
1. `DepositToken.transfer`/`transferFrom`/`seize` all funnel into `_transfer`, which calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance was 0 (`contracts/DepositToken.sol:517-520`). There is no opt-in; the recipient cannot refuse.
2. `Pool.addToDepositTokensOfAccount` is protected by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148`). The same modifier guards `addToDebtTokensOfAccount`.
3. The attacker deposits minimum amounts into each of the pool's (up to 30) `DepositToken`s to obtain dust, then calls `transfer(victim, 1)` on each. Each transfer appends an entry to the victim's set.
4. After 30 entries, any subsequent action that adds a new token to the victim's account reverts:
   - `DepositToken.deposit`/`_mint` for a collateral the victim does not yet hold (`_mint` calls `addToDepositTokensOfAccount` on zero prior balance, `contracts/DepositToken.sol:485-488`).
   - `DebtToken.issue`/`mint` for a new synthetic (calls `addToDebtTokensOfAccount`).
   - Any incoming `DepositToken` transfer of a new token, including liquidator-free paths — the victim also cannot be saved by others depositing on their behalf.
   - Critically, a victim with an unhealthy position cannot deposit *new* collateral types to restore health, so the grief converts into forced liquidation when their existing collateral set is insufficient.
5. Escape requires the victim to fully zero out dust balances token-by-token (30+ transactions, each front-runnable by the attacker re-dusting a cleared slot), so the freeze persists as long as the attacker is willing to spend dust.

### Impact Explanation
Temporary freezing of funds / forced liquidation: the victim's `deposit`, `issue`, and incoming-transfer paths revert, preventing them from adding collateral to an at-risk position. An attacker monitoring `debtPositionOf` can dust-fill slots of accounts nearing the collateral factor and block top-ups, then liquidate them for the `liquidatorIncentive` bonus (`Pool.liquidate`, `contracts/Pool.sol:537+`). For accounts whose only collateral types they already hold, impact is limited to blocking new collateral types, but for multi-collateral rescues it is a direct liveness break.

### Likelihood Explanation
Requires only EOA calls to public `deposit` and `transfer`. Cost is bounded by dust deposits in up to 30 collateral types plus ~30 cheap transfers. No privileged role, oracle manipulation, or external dependency is needed — `MAX_TOKENS_PER_USER`, the modifier, and the auto-add hook all execute on the deployed configuration. Mitigating factor: pool must list several deposit tokens (capped at 30 by `addDepositToken`, `contracts/Pool.sol:703`) and the victim can eventually clear slots, making this a temporary freeze rather than permanent.

### Recommendation
- Only register a token in `depositTokensOfAccount` on explicit user action (e.g., inside `deposit`, not inside generic `_transfer`/`seize`), or let recipients opt in.
- Alternatively, bound griefing by allowing users to remove a token entry via an explicit `Pool.removeFromDepositTokensOfAccount`-style call even with nonzero balance after transferring, or skip the revert and silently not track dust below a threshold.
- Keep `MAX_TOKENS_PER_USER` accounting symmetric: count debt and deposit slots independently so filling one side cannot block the other.

### Proof of Concept
Hardhat fork sketch:

```ts
// Pool has >=2 deposit tokens listed; MAX_TOKENS_PER_USER = 30
const victim = alice.address;

// Attacker deposits dust into each listed deposit token to get balances
for (const dt of depositTokens) {
  await underlying.connect(attacker).approve(dt.address, DUST);
  await dt.connect(attacker).deposit(DUST); // attacker now holds dust msTokens
}

// Grief: fill victim's slots — each transfer adds an entry via
// DepositToken._transfer -> pool.addToDepositTokensOfAccount
for (const dt of depositTokens) {
  await dt.connect(attacker).transfer(victim, 1);
}
// Repeat across deposit tokens until getDepositTokensOfAccount(victim).length
// + getDebtTokensOfAccount(victim).length == 30

// Victim tries to deposit a collateral type they don't yet hold
await otherUnderlying.connect(alice).approve(newDt.address, amount);
await expect(newDt.connect(alice).deposit(amount))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Same for issuing a new synthetic debt
await expect(debtToken.connect(poolSigner).issue(amount, victim))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Victim's unhealthy position cannot be topped up -> liquidate succeeds
// while deposits keep reverting.
```

Note: I verified the revert path in `Pool.onlyIfAdditionWillNotReachMaxTokens`, `DepositToken._transfer`/`_mint`, and the existing test `should revert when reach max tokens` (`test/Pool.test.ts:1491-1521`) which confirms the revert behavior is live code, not mocked-only.