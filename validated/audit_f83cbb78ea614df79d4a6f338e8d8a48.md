### Title
Attacker can fill a victim's per-account token list to `MAX_TOKENS_PER_USER` via dust deposits/transfers, blocking the victim from opening new positions - (File: contracts/Pool.sol)

### Summary
Analogous to CVE-2025-38501 (repeated same-identity connections exhausting a shared connection cap), Metronome enforces a per-account cap `MAX_TOKENS_PER_USER` on the combined length of `depositTokensOfAccount` + `debtTokensOfAccount`, enforced in `onlyIfAdditionWillNotReachMaxTokens` at `Pool.sol:143-148`. Because `DepositToken._mint` and `DepositToken._transfer` unconditionally push the token into the *recipient's* list (`DepositToken.sol:486-488`, `DepositToken.sol:518-520`), and `deposit(amount_, onBehalfOf_)` lets anyone mint msdTOKEN to an arbitrary victim (`DepositToken.sol:211-237`), an unprivileged attacker can repeatedly deposit 1-wei dust of every listed collateral `onBehalfOf` a victim — or dust-`transfer` msdTOKENs — until the victim's list hits the cap. There is no consent check, no minimum amount, and no per-recipient opt-in.

### Finding Description
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (`Pool.sol:143-148`, `204-220`).
- `DepositToken._transfer` adds the token to `recipient_`'s list when the recipient's prior balance was 0 (`DepositToken.sol:517-520`). `transfer`/`transferFrom` only check the *sender's* unlocked balance (`DepositToken.sol:348-376`).
- `DepositToken.deposit` mints to `onBehalfOf_` chosen by the caller (`DepositToken.sol:234`), so no victim signature is needed — the attacker only pays dust underlying to the Treasury.
- Once full, every subsequent `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` for the victim reverts, so `deposit` of any collateral type the victim doesn't already hold reverts, and `DebtToken.issue`/`mint` reverts at `DebtToken._mint` → `pool.addToDebtTokensOfAccount` (`DebtToken.sol:597-600`) for any synthetic the victim hasn't borrowed before. Transfers of new msdTOKENs to the victim also revert.

### Impact Explanation
Temporary freezing of funds / liveness break: the victim is permanently blocked (until they manually empty dust positions one-by-one) from depositing any collateral type not already in their list and from issuing any new synthetic debt. A victim whose position is near liquidation and whose rescue path requires a *different* collateral (e.g., their existing collateral depegged or hit `maxTotalSupply`) cannot improve health and becomes liquidatable — indirect loss of funds. Forced dust balances also misattribute collateral ownership to the victim. Attack cost is only gas + ~1 wei of each underlying per slot; debt-token slots can't be pushed (debt tokens are non-transferable, `DebtToken.sol:507-519`), so feasibility requires the pool to list enough deposit tokens to reach `MAX_TOKENS_PER_USER` (the constant's value vs. number of listed collaterals determines practicality — I could not confirm the constant's deployed value within this pass; the test at `test/Pool.test.ts:1386-1416` fills it with `max/2` deposit + `max/2` debt tokens).

### Likelihood Explanation
Unprivileged EOA; no privileged role, oracle manipulation, or governance change needed. Attack is repeatable and front-runnable (attacker can re-fill slots faster than the victim clears them, since clearing requires victim transactions while filling requires only attacker transactions). Likelihood is bounded by the number of distinct deposit tokens offered by the pool relative to `MAX_TOKENS_PER_USER` — if offerings are far below the cap, the attack is infeasible today but becomes feasible as new collaterals are listed.

### Recommendation
Only add a token to `depositTokensOfAccount`/`debtTokensOfAccount` when the recipient initiated or consented (e.g., skip list insertion on inbound `transfer` to an account that never deposited, or track a `hasDeposited` flag set only via `deposit`/`issue`). Alternatively, allow victims to remove entries permissionlessly (e.g., a `removeDustToken` that force-burns sub-dust balances), or exempt `transfer`-induced additions from the cap accounting.

### Proof of Concept
Reproducible Hardhat sketch (fork/test env):

```ts
// setup: pool with N listed deposit tokens D1..DN where N >= MAX_TOKENS_PER_USER
const victim = bob.address
for (const d of depositTokens) {
  await underlying(d).approve(d.address, 1)
  // attacker deposits 1 wei of each underlying, minting dust msdTOKEN to victim
  await d.connect(attacker).deposit(1, victim)
  // or, if attacker already holds msdTOKEN: await d.connect(attacker).transfer(victim, 1)
}
// victim now blocked:
await expect(newCollateral.connect(bob).deposit(amount, victim))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens') // propagates from addToDepositTokensOfAccount
await expect(newDebtToken.connect(bob).issue(amount, bob.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens') // propagates from addToDebtTokensOfAccount
```

Mirrors the existing cap test at `test/Pool.test.ts:1386-1416`, but with the additions initiated by an attacker against a non-consenting account rather than by the token contracts for a cooperating account.