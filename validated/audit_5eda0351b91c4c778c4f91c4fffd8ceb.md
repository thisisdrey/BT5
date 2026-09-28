### Title
Unprivileged attacker can grief any account's token list via dust `DepositToken` transfers, temporarily DoSing deposits, borrows, and collateral top-ups - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` enforces `MAX_TOKENS_PER_USER = 30` as a combined cap on an account's tracked deposit tokens and debt tokens. Because `DepositToken._transfer` (reachable via public `transfer`/`transferFrom`) unconditionally calls `Pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes 0 → >0, any unprivileged user can push dust balances of every listed deposit token onto a victim's list. Once the victim's combined list hits 30 entries, every subsequent action that adds a new token — depositing a new collateral, minting a new synthetic debt token, receiving a deposit-token transfer — reverts with `UserReachedMaxTokens`.

### Finding Description
In `Pool.sol`, `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`Pool.sol:143-148`, `204-220`).

`DepositToken._transfer` (`DepositToken.sol:517-525`) adds the recipient to the tracked list on any nonzero incoming balance. `transfer` only validates the *sender's* unlocked balance via `_revertIfLocked(_msgSender, amount_)` (`DepositToken.sol:348-354`); the recipient has no way to opt out.

Attack path (fully unprivileged):
1. Attacker deposits a small amount of each supported underlying via `DepositToken.deposit(amount_, attacker)` to obtain ≥1 wei of every `msd*` deposit token in the pool (net of `depositFee`).
2. For each deposit token `t_i`, attacker calls `t_i.transfer(victim, 1)` — each call adds `t_i` to `victim`'s `depositTokensOfAccount` (`DepositToken.sol:518-520`).
3. Repeat until `victim` reaches 30 tracked tokens (deposit tokens + any existing debt tokens).
4. Thereafter, `victim.deposit(newCollateral)` reverts inside `_mint → addToDepositTokensOfAccount` (`DepositToken.sol:485-488`), `DebtToken.issue/mint` for a new synthetic reverts inside `addToDebtTokensOfAccount`, and any transfer of a deposit token the victim does not already hold reverts.

Modifiers don't help: `transfer` has no `nonReentrant`/`whenNotPaused` restriction that blocks this, `SynthContext._msgSender` is irrelevant since the attacker calls the deposit tokens directly, and there is no minimum-transfer or whitelist check.

### Impact Explanation
This is the availability/DoS bug class of CVE-2017-3308 mapped onto Metronome's account-list invariant. Concretely:

- A target user cannot deposit a *new* collateral type, cannot open debt in a *new* synthetic, and cannot receive deposit-token transfers — a temporary freeze of the account's ability to use the protocol.
- Timed griefing: an attacker can watch the mempool and front-run a user's first deposit, or stuff the list of an account that urgently needs to add collateral to stay healthy. The victim's only escape is to manually `transfer` each dust token back out (each removal requires balance → 0, `DepositToken.sol:522-525`), paying gas for up to 30 transactions while remaining blocked in the meantime — during which their position may be liquidated because they cannot add the collateral needed to restore health.
- Funds are not permanently lost, matching the "temporary freezing of funds" acceptance category, analogous to the CVE's availability-only impact.

### Likelihood Explanation
- Attacker needs only an EOA and dust deposits across the pool's listed deposit tokens — no privileged role, no oracle manipulation, no governance action.
- Effectiveness depends on deployed configuration: the attack requires enough registered deposit tokens (plus the victim's existing debt tokens) to reach `MAX_TOKENS_PER_USER = 30`. On deployments where the combined reachable token count is lower, the cap cannot be hit and the attack fails; this must be checked per pool before weighting likelihood.
- Cost to the attacker is bounded (dust deposits + one `transfer` call per token); deposit fees may force slightly larger deposits so the minted balance is > 0, but the attacker's own deposits remain withdrawable afterward.

### Recommendation
- Do not add to a recipient's tracked set on ordinary `transfer`/`transferFrom`, or allow recipients to be added only via `deposit`/`seize` paths; alternatively make `addToDepositTokensOfAccount` not revert on cap for unsolicited transfers (e.g., skip tracking but still move balance).
- Allow forced cleanup: let a user remove a token from their own list via a `sweep`/`remove` function even while their balance is nonzero, or let `removeFromDepositTokensOfAccount` be callable for any token with dust balance below a threshold.
- Alternatively, transfer dust tokens' accounting so that a balance can exist without occupying a tracked slot (health evaluation would then need an explicit opt-in list).

### Proof of Concept
Hardhat-style PoC (against deployed pool `p`, listed deposit tokens `dt[0..n)`):

```ts
// Attacker deposits dust of each underlying and pushes 1 wei to victim
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, DUST);
  await dt.connect(attacker).deposit(DUST, attacker.address); // mints >=1 wei msdToken
  await dt.connect(attacker).transfer(victim.address, 1);     // victim's list += dt
}
// After victim reaches MAX_TOKENS_PER_USER:
await expect(
  someOtherDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
await expect(
  debtToken.connect(victim).issue(1) // new synthetic debt
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Reproduced directly by the existing unit-test pattern in `test/Pool.test.ts:1386-1416`, which confirms `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once 30 entries are reached — the missing step in that test is that the entries can be populated by an *attacker* via `DepositToken.transfer`, not only by the account owner.