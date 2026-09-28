### Title
Attacker can stuff a victim's per-account token list with dust deposits/transfers to hit `MAX_TOKENS_PER_USER`, permanently blocking new collateral deposits and inbound transfers until manually cleaned - (File: contracts/Pool.sol)

### Summary
`Pool` tracks, per account, the set of `depositTokensOfAccount` and `debtTokensOfAccount`, capped at `MAX_TOKENS_PER_USER = 30`. Entries are added as a side effect of `DepositToken._transfer` (used by `transfer`, `transferFrom`, `seize`) and `_mint` (used by `deposit`) whenever the recipient's balance was previously zero. Any unprivileged user can push arbitrary dust amounts of a deposit token to any victim address — no opt-in required. By cycling through all distinct deposit tokens in the pool (or across pools, since the lists are per-pool but the attacker needs only one pool to fill it), an attacker fills the victim's 30 slots, after which every operation that would add a *new* token type to the victim's list reverts with `UserReachedMaxTokens`.

### Finding Description
- `Pool.addToDepositTokensOfAccount` enforces the cap via `onlyIfAdditionWillNotReachMaxTokens` (contracts/Pool.sol:143-148, 216-220).
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` for any first-time recipient (contracts/DepositToken.sol:518-520); `transfer`/`transferFrom` only require the *sender's* unlocked balance (lines 348-376), so a third party can send dust to any victim.
- `deposit(amount_, onBehalfOf_)` mints to an arbitrary beneficiary (line 234), so dust can be minted directly to the victim without needing to acquire the token first.
- Result: once the victim holds dust balances of N distinct deposit tokens, `deposit` into any *new* collateral type reverts (mint → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`), `transfer`/`transferFrom`/`seize` of any new token type to the victim reverts, and if the victim's full balance is locked against debt (`unlockedBalanceOf` == 0, lines 383-398), they cannot even transfer the dust out themselves — making the freeze permanent for that account.

### Impact Explanation
This is a direct availability/integrity analog to the CVE's "hang + unauthorized data modification" class: the attacker mutates the victim's account state without authorization (unrequested list entries) and thereby denies service. Concretely:
- A victim with an unhealthy position cannot top up with a new collateral type (and may be unable to clear the dust if locked), forcing liquidation that would otherwise be avoidable — indirect loss of the liquidation fee/collateral.
- Any inbound `transferFrom`/`seize` of a new token type to the victim reverts, blocking integrations (e.g., SmartFarmingManager flows crediting new deposit tokens) — temporary-to-permanent freezing of funds depending on lock state.

### Likelihood Explanation
Cost is proportional to the number of distinct deposit tokens needed (≤30) and requires only dust amounts of each underlying. No privileged role, oracle manipulation, or flash loan is needed; standard EOA calls suffice. The attack is cheap and repeatable on every deployed pool.

### Recommendation
- Make additions to `depositTokensOfAccount`/`debtTokensOfAccount` opt-in or self-only (e.g., only register on `deposit`/issue initiated for one's own account), or
- Track balances implicitly (skip the set entirely for forced inbound transfers), or
- Allow the cap check to be bypassed on removal/self-cleanup, and consider a minimum-amount threshold for list insertion.

### Proof of Concept
Hardhat (pool with ≥2 deposit tokens; assume pool already lists many deposit tokens or attacker iterates `pool.getDepositTokens()`):

```ts
// attacker fills victim's list with dust
const tokens = await pool.getDepositTokens(); // N distinct DepositTokens
for (const t of tokens.slice(0, 30)) {
  const dt = await ethers.getContractAt('DepositToken', t);
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying());
  // cheapest path: attacker deposits dust onBehalfOf victim
  await underlying.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, victim.address); // adds dt to victim's list
}
// victim's list is now at MAX_TOKENS_PER_USER = 30
// victim tries to deposit a collateral type they don't already hold
const newDt = await ethers.getContractAt('DepositToken', tokens[30]);
await underlying2.connect(victim).approve(newDt.address, amt);
await expect(
  newDt.connect(victim).deposit(amt, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
// inbound transfer of any new token type to victim also reverts
await expect(
  newDt.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Note: I did not fully verify whether `DebtToken.issue` permits adding debt tokens to arbitrary `to_` accounts (which would be an even stronger variant, forcing debt onto victims); the deposit-token path alone is sufficient for the finding.