### Title
Griefing DoS: attacker fills a victim's `MAX_TOKENS_PER_USER` slots via dust `deposit(onBehalfOf)`/transfers, blocking all new deposits and borrowing - ([File: contracts/Pool.sol])

### Summary
`Pool` enforces a per-account cap of `MAX_TOKENS_PER_USER = 30` on the combined length of `depositTokensOfAccount` + `debtTokensOfAccount` (Pool.sol:79, 143-148). Every time an account's `DepositToken` balance goes from `0` to positive — via `deposit(amount, onBehalfOf)` minting to an arbitrary `onBehalfOf_`, via `transfer`/`transferFrom`, or via `seize` — `Pool.addToDepositTokensOfAccount` is called and reverts with `UserReachedMaxTokens` once the cap is hit (DepositToken.sol:486-488, 518-520; Pool.sol:216-220). The same applies on the debt side via `DebtToken` → `addToDebtTokensOfAccount` (Pool.sol:204-208). The recipient cannot opt out of being added, and the sender controls both `onBehalfOf_` and the transfer recipient.

### Finding Description
An unprivileged attacker fills a target account's token list at negligible capital cost:

1. For each `DepositToken` registered in the pool (the pool itself is capped at the same `MAX_TOKENS_PER_USER` bound — `addDepositToken` reverts `ReachedMaxDepositTokens`, see test/Pool.test.ts:1236-1256), the attacker calls `deposit(1 wei, victim)` on each token, or deposits dust to themselves and calls `transfer(victim, 1 wei)`. Each call adds that token to `depositTokensOfAccount[victim]` because `_balanceBefore == 0 && amount > 0` (DepositToken.sol:517-520).
2. Once the victim's combined list reaches 30 entries, every subsequent operation that would add a *new* token to their list reverts:
   - `DepositToken.deposit` into any collateral they don't already hold → `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
   - `DebtToken.issue`/`mint` (and `SmartFarmingManager.leverage`) for any new synthetic → `addToDebtTokensOfAccount` → `UserReachedMaxTokens`.
   - Receiving `seize` proceeds as a liquidator into a new token type → `addToDepositTokensOfAccount` reverts (liquidator can rotate addresses, so this is secondary).
3. The attacker can repeat the griefing indefinitely: if the victim spends gas transferring the dust out to free slots (`_transfer` removes entries when the sender's balance hits 0, DepositToken.sol:523-525), the attacker re-fills them, since there is no minimum amount and no way to reject incoming balances.

This mirrors the reported bug class: a hard slot limit that an unprivileged user can saturate for someone else, making a protocol feature unusable.

### Impact Explanation
Temporary denial of service / freezing of functionality for the victim: they cannot deposit into new collateral types, cannot open debt positions in new synthetics, and cannot leverage — all core entry points (`deposit`, `issue`, `leverage`, `swap`+issue paths) revert until they manually evict the dust entries at their own gas cost. Existing balances and withdrawals remain usable, so it is a liveness/availability impact rather than theft, consistent with the original Medium-severity finding. Full lockout requires the pool to have enough registered `DepositToken`s (up to 30) for the attacker to occupy all slots; pools with fewer registered tokens only partially consume the victim's budget — every dust token the attacker pushes permanently reduces the victim's headroom for legitimate use.

### Likelihood Explanation
- Requirements: attacker only needs dust amounts of each pool's underlying collateral (recoverable via `withdraw` afterward, modulo deposit/withdraw fees), and public entry points only — `deposit(amount_, onBehalfOf_)` or `transfer`. No privileged role needed.
- The check is unconditional (`onlyIfAdditionWillNotReachMaxTokens` on Pool.sol:143-148) with no exemption, no minimum deposit amount, and no consent mechanism on the recipient side.
- Effectiveness scales with the number of governor-registered deposit/debt tokens in the pool; a pool with 30 deposit tokens allows complete victim lockout.

### Recommendation
- Only add a token to the account list when the received amount exceeds a meaningful minimum (e.g., a USD floor checked via `masterOracle`), so dust griefing is ineffective.
- Alternatively, make slot additions recipient-opt-in: auto-add only on `deposit` where `onBehalfOf_ == _msgSender()`, or skip list-add on `transfer`/`seize` and lazily enumerate holdings via the pool's token list when computing `depositOf`/`debtPositionOf`.
- Allow an account to forcibly "sweep" zero/dust list entries, or remove the per-account cap in favor of iterating the pool-level token set (the arrays are bounded by `MAX_TOKENS_PER_USER` anyway on the registration side).

### Proof of Concept
Hardhat (existing smock-based harness in `test/Pool.test.ts` is directly adaptable — see test/Pool.test.ts:1386-1416 which already demonstrates the revert at cap):

```ts
// pool with N registered DepositTokens; victim starts with 0 positions
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber() // 30

// Attacker fills every free slot of the victim with dust deposits
for (const dt of depositTokens) {
  // attacker holds dust underlying; deposit on behalf of victim
  await underlying.connect(attacker).approve(dt.address, 1)
  await dt.connect(attacker).deposit(1, victim.address) // adds dt to victim's list
}

// Victim's combined list is now full
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(max)

// Any new-token operation by the victim reverts
await expect(
  newDepositToken.connect(victim).deposit(parseEther('10'), victim.address)
).to.revertedWithCustomError(pool, 'UserReachedMaxTokens')

await expect(
  debtToken.connect(victim).issue(parseEther('1'), victim.address)
).to.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Caveat: a fork PoC against a deployed pool must confirm that pool's registered deposit/debt token count is sufficient to saturate 30 slots; if fewer exist, the attack only reduces headroom proportionally (each attacker's dust token permanently consumes one victim slot until evicted).