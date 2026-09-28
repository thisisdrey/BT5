### Title
Attacker fills victim's per-account token list to `MAX_TOKENS_PER_USER` via dust `deposit(onBehalfOf_)`, DoSing deposits, borrows, and liquidations - (File: contracts/Pool.sol)

### Summary
Analogous to the Discourse unlimited-drafts DoS (unbounded attacker-created storage entries that exhaust a resource), `Pool` lets anyone grow another account's `depositTokensOfAccount` set without that account's consent. `DepositToken.deposit(amount_, onBehalfOf_)` mints to an arbitrary `onBehalfOf_`, and `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance goes from 0 to non-zero. `addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER = 30` and reverts with `UserReachedMaxTokens` once the combined `debtTokensOfAccount` + `depositTokensOfAccount` length hits 30 (contracts/Pool.sol:143-148, 216-220; contracts/DepositToken.sol:211-237, 486-488).

### Finding Description
An unprivileged attacker can:

1. For each of the pool's registered `DepositToken`s, call `deposit(1 wei of underlying, victim)` (or `transfer(victim, 1)` with dust msdTOKEN already held).
2. Each call hits `_mint` → `pool.addToDepositTokensOfAccount(victim)` → `depositTokensOfAccount.add(victim, token)`, permanently adding an entry (removal only happens if victim's balance returns to 0).
3. Once `debtTokensOfAccount.length(victim) + depositTokensOfAccount.length(victim) == 30`, every subsequent add reverts in `onlyIfAdditionWillNotReachMaxTokens`.

The same forced-add path exists in `DepositToken._transfer` (depositing/transferring dust to the victim) and in `DebtToken` issuance if debt is issued to the account. There is no opt-out: the victim cannot refuse incoming deposit-token dust, and each distinct registered deposit token is a free "draft key" the attacker can write into the victim's storage entry.

### Impact Explanation
Once a victim's list is saturated:

- **Deposits of any new collateral type revert.** `deposit(..., victim)` for a token not already in the victim's list reverts in `addToDepositTokensOfAccount`, so the victim cannot onboard new collateral. A victim with zero existing positions is permanently locked out of the pool under that address.
- **New borrows revert.** `DebtToken.issue` (which calls `addToDebtTokensOfAccount`) reverts for any synthetic the victim doesn't already hold debt in, blocking position management during price moves — a victim who can only add collateral to existing token types may be unable to restore health if their existing set is exhausted.
- **Liquidations can be bricked.** `Pool.liquidate` seizes collateral to the liquidator via `DepositToken.seize` → `_transfer` → `addToDepositTokensOfAccount(liquidator)`. An attacker can pre-fill a known liquidator/keeper EOA or liquidation contract's list with dust, so `seize` of any deposit token not already in that list reverts, blocking liquidation of underwater positions and allowing bad debt to accrue to the protocol.

This maps to temporary/permanent freezing of funds and potential protocol insolvency — the victim's ability to deposit, borrow, or be liquidated is gated by attacker-controlled storage writes, exactly the "unbounded attacker-created resource" class of the reference bug.

### Likelihood Explanation
- Attacker is fully unprivileged: `deposit`, `transfer`, and liquidation seize paths are all public entry points reachable directly or via `Operator.execute`.
- Cost is proportional to the number of distinct deposit tokens in the pool: filling the list requires up to 30 dust deposits/transfers. Feasibility therefore depends on the deployed pool having a meaningful number of registered `DepositToken`s; Metronome Synth pools enumerate many collaterals (Vesper/vToken wrappers etc.), and `depositTokens`/`debtTokens` are only capped for the *account* (30), not for the pool. If a pool has ≥30 registered deposit+debt tokens, the attack is fully executable; if it has fewer, the cap may be unreachable and the severity degrades. This configuration dependency is the main uncertainty.
- No modifier stops it: `addToDepositTokensOfAccount` is callable by any registered deposit token, `deposit` is `whenNotPaused nonReentrant` only, and there is no per-account allowlist or minimum-deposit threshold (only `amount_ == 0` is rejected, dust amounts pass).

### Recommendation
- Do not let arbitrary senders grow another account's token set: in `DepositToken.deposit`, either restrict `onBehalfOf_` additions (e.g., only add when `onBehalfOf_ == _msgSender()` or require the recipient to pull/claim), or skip `addToDepositTokensOfAccount` when the minted amount is below a dust threshold.
- Make `_transfer`/`seize` tolerant of a full recipient list: instead of reverting in `addToDepositTokensOfAccount`, catch the failure or allow the transfer while flagging the position off-list (e.g., track balances independently of the enumeration used for health checks).
- Alternatively, raise/remove `MAX_TOKENS_PER_USER` and bound `debtPositionOf` gas by iterating a fixed, pool-level token list rather than per-account sets.

### Proof of Concept
Hardhat (fork) outline — reproducible against deployed pool addresses:

```ts
// Attacker: fill victim's list
const pool = await ethers.getContractAt('Pool', POOL);
const depositTokens = await pool.getDepositTokens(); // registered collaterals
const victim = LIQUIDATOR_OR_VICTIM; // arbitrary target address

for (const dt of depositTokens.slice(0, 30)) {
  const depositToken = await ethers.getContractAt('DepositToken', dt);
  const underlying = await ethers.getContractAt('IERC20', await depositToken.underlying());
  // acquire dust underlying (or dust msdTOKEN) then:
  await underlying.approve(depositToken.address, 1);
  await depositToken.deposit(1, victim.address); // adds dt to victim's set
}

// Victim list now == MAX_TOKENS_PER_USER
// 1) Any new deposit to victim reverts:
await expect(newDepositToken.deposit(1e18, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
// 2) Liquidation crediting a not-yet-held token to the filled liquidator reverts
//    inside seize -> addToDepositTokensOfAccount.
```

The invariant broken is liveness: attacker-chosen arguments to `deposit`/`transfer`/`seize` deterministically revert the victim's subsequent legitimate calls, per the `onlyIfAdditionWillNotReachMaxTokens` check (contracts/Pool.sol:143-148) triggered from `_mint`/`_transfer` (contracts/DepositToken.sol:486-488, 517-520).