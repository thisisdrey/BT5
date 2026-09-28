### Title
Griefing via dust transfers fills victim's token list to `MAX_TOKENS_PER_USER`, DoS-ing deposits into new collaterals and issuance of new synths - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to CVE-2022-21362 (availability-only DoS), an unprivileged attacker can degrade a victim's ability to use the protocol by filling the victim's per-account token list up to `MAX_TOKENS_PER_USER = 30` with dust `DepositToken` transfers. Once full, every code path that adds a new token entry for that account — `DepositToken.deposit`, `DepositToken.transfer`/`seize` to the victim, and `DebtToken._mint` via `issue`/`mint` — reverts with `UserReachedMaxTokens`, blocking the victim from depositing new collateral types and from issuing new synthetic-asset debt.

### Finding Description
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` are guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts when `debtTokensOfAccount.length(account) + depositTokensOfAccount.length(account) >= 30` (`contracts/Pool.sol:143-148`, `contracts/Pool.sol:204-220`).

`DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance transitions from zero (`contracts/DepositToken.sol:517-520`). `DepositToken.transfer`/`transferFrom` are permissionless and only check the *sender's* unlocked balance (`_revertIfLocked(sender_, amount_)`, `contracts/DepositToken.sol:348-376`). There is no opt-in or minimum amount on the receiving side, so anyone can push dust msdTOKEN balances onto an arbitrary victim.

`DebtToken._mint` calls `pool.addToDebtTokensOfAccount(account_)` when debt is first created for an account (`contracts/DebtToken.sol:597-600`), and `DebtToken.issue` mints debt to `_msgSender` (`contracts/DebtToken.sol:235-271`). Debt tokens are non-transferable (`contracts/DebtToken.sol:507-519`), so the victim cannot be force-fed debt entries, but once the shared 30-slot budget is consumed by deposit-token dust, `issue` for any *new* synthetic type also reverts inside `addToDebtTokensOfAccount`.

Attack trace (unprivileged EOA):
1. Attacker deposits minimal amounts of each registered collateral via `DepositToken.deposit(amount, attacker)` (or via `NativeTokenGateway`/`VesperGateway`).
2. Attacker calls `DepositToken.transfer(victim, 1)` (or smallest unit) for each deposit token, plus uses `deposit(smallAmount, victim)` on behalf of the victim, until `depositTokensOfAccount[victim]` reaches 30.
3. Now: `deposit(x, victim)` for any new collateral reverts in `_mint` → `addToDepositTokensOfAccount`; `issue(amount, to)` reverts in `_mint` → `addToDebtTokensOfAccount`; `transfer`/`transferFrom`/`seize` of any deposit token the victim doesn't yet hold reverts in `_transfer` → `addToDepositTokensOfAccount`.

### Impact Explanation
Availability/DoS, matching the CVE's impact class:
- Victim cannot open positions in new collateral or new synthetic assets — all minting paths revert.
- A victim approaching liquidation cannot diversify into a new collateral type to restore health (they can still add collateral they already hold or repay, so existing funds are not permanently frozen).
- Liquidation `seize` payouts addressed to a griefed recipient account revert, though a liquidator can simply use a clean receiver, so this does not stall liquidations protocol-wide.
- The DoS is temporary: the victim recovers a slot by fully withdrawing/burning one dust deposit token (balance → 0 triggers `removeFromDepositTokensOfAccount`, `contracts/DepositToken.sol:459-462`). It costs the victim gas and forced churn, and must be repeated if the attacker re-griefs.

### Likelihood Explanation
Fully reachable by any EOA using only public entry points (`DepositToken.deposit`, `DepositToken.transfer`). No privileged role, oracle manipulation, or governance action required. Cost is bounded by gas for ~30 deposits/transfers plus negligible dust value. However, impact is bounded — it is griefing/liveness degradation rather than theft or permanent freezing — consistent with the Medium-severity, availability-only nature of the reference CVE.

### Recommendation
Do not couple the per-account list cap to unsolicited inbound transfers. Options:
- Count only tokens the user explicitly engaged with (e.g., only add on `deposit`/`issue`, not on `transfer`-received balances), accepting slightly stale `debtPositionOf` for pure transfer-in balances; or
- Make `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` fail-open for the recipient (e.g., skip adding instead of reverting, or let the recipient's list exceed the cap while capping only `issue`/`deposit` initiations); or
- Charge a meaningful minimum first-balance threshold so dust cannot occupy slots cheaply.

### Proof of Concept
Hardhat-style reproduction against deployed configuration:

```ts
// Fork a chain where `pool` has >= 30 deposit tokens registered (or the
// attacker pre-mints coverage via deposit() for each listed DepositToken).
const victim = await ethers.getSigner(VICTIM);

// 1. Attacker deposits dust in each deposit token, then pushes it to victim.
for (const dt of depositTokens) {
  await underlying.connect(attacker).approve(dt.address, dustAmount);
  await dt.connect(attacker).deposit(dustAmount, attacker.address); // or deposit(dust, victim)
  await dt.connect(attacker).transfer(victim.address, 1);
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30);

// 2. New collateral deposit on behalf of victim now reverts.
await expect(newDt.connect(attacker).deposit(1e6, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 3. Victim cannot issue a new synthetic type (debt token not yet in list).
await expect(newDebtToken.connect(victim).issue(minAmount, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// 4. Existing positions still work; freeing a slot restores functionality.
await dt0.connect(victim).withdraw(1, victim.address); // burns dust, removes entry
await newDt.connect(attacker).deposit(1e6, victim.address); // succeeds now
```

Caveat: this is a temporary-funds/liveness DoS and the victim retains a self-recovery path (withdrawing a dust token frees a slot), so severity is limited to griefing — consistent with the Medium/availability-only profile of the source CVE rather than a fund-loss bug.