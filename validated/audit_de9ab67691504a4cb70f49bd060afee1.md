### Title
Unprivileged attacker can fill a victim's collateral/debt token list via dust `DepositToken` transfers, DoS-ing deposits, issuance, and liquidations - (File: contracts/Pool.sol)

### Summary
`Pool` caps each account's combined deposit+debt token count at `MAX_TOKENS_PER_USER = 30` and reverts with `UserReachedMaxTokens` once the cap is hit (`Pool.sol:143-148`). Entries are added permissionlessly as a side effect of `DepositToken._transfer`/`_mint`, which push the recipient into `depositTokensOfAccount` whenever the recipient's balance was zero (`DepositToken.sol:518-520`, `DepositToken.sol:486-488`). Since `DepositToken.transfer`/`transferFrom` are public and only restricted by the sender's unlocked balance, any EOA can dust-transfer tiny amounts of each registered `DepositToken` into a victim, permanently occupying their slots until the victim manually clears each dust balance.

### Finding Description
The bug class from the external report (uncontrolled resource consumption → DoS triggered by a crafted request) maps onto Metronome's per-account token registry:

- `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`Pool.sol:204-220`).
- Every first-time receipt of a `DepositToken` registers it: `_mint` on `deposit()` (`DepositToken.sol:486-488`) and `_transfer` on `transfer`/`transferFrom`/`seize` (`DepositToken.sol:518-520`).
- Removal only happens when the holder's balance returns to zero (`DepositToken.sol:460-462`, `DepositToken.sol:523-525`), which the victim must do one token at a time.

Attack path (no privileges needed):
1. Attacker deposits dust amounts of N registered collaterals (or already holds them).
2. Attacker calls `DepositToken.transfer(victim, 1 wei)` for each token until `depositTokensOfAccount.length(victim) + debtTokensOfAccount.length(victim) == 30`.
3. Consequences:
   - `victim.deposit()` of any *new* collateral type reverts in `_mint → addToDepositTokensOfAccount` (`DepositToken.sol:234`, `Pool.sol:216`).
   - `DebtToken.issue()` for any *new* synthetic reverts in `_mint → addToDebtTokensOfAccount` (`DebtToken.sol:598-600`), freezing the victim's borrowing even though their collateral is healthy.
   - `Pool.liquidate` seizes via `DepositToken.seize → _transfer(liquidator)`, which calls `addToDepositTokensOfAccount(liquidator)`. By dusting a liquidator (front-runnable, since the liquidator is the `liquidate` sender observable in the mempool), the attacker can make `liquidate` revert, temporarily DoS-ing liquidation of an unhealthy position.

No existing guard stops this: `onlyIfAdditionWillNotReachMaxTokens` is the cap itself, the reentrancy guard doesn't apply cross-transactionally, and `whenNotPaused`/`whenNotShutdown` are irrelevant to normal operation. `DebtToken` transfers are disabled (`DebtToken.sol:507-519`), so the debt side can't be dusted directly, but the deposit side is fully attacker-controllable.

### Impact Explanation
Denial of service against a targeted account: inability to deposit new collateral types, inability to open new debt positions (`issue` always reverts), and, by pre-filling a liquidator's list, temporary DoS of `Pool.liquidate` — which can let an unhealthy position survive liquidation attempts and accrue bad debt. Impact is "temporary freezing" rather than permanent loss because the victim can recover by transferring/withdrawing each dust balance to drop below 30, but each cleanup costs gas, and the liquidation-blocking variant costs the attacker nothing per attempt while protecting an insolvent position during a mempool race.

### Likelihood Explanation
Requires the pool to have enough distinct registered `DepositToken`s for the attacker to fill the victim's remaining slots (victim's existing deposits+debts count toward the 30). Feasibility is conditional on deployment configuration — on pools with many collaterals or victims already holding several positions, cheap dust deposits make this trivial. The liquidation-blocking variant additionally requires the attacker to predict the liquidator address, feasible only via mempool front-running against an EOA liquidator. Conditional likelihood, but reachable purely by unprivileged EOAs through public entry points.

### Recommendation
- Decouple the cap from unsolicited receipts: only enforce `MAX_TOKENS_PER_USER` on user-initiated actions (`deposit`, `issue`), not on inbound `transfer`/`seize` registrations — e.g., let `addToDepositTokensOfAccount` silently skip or store uncapped on transfers while gating `deposit()`/`issue()` on the caller's own list size.
- Alternatively, allow `seize` to bypass the cap so liquidations can never be blocked by the recipient's list.
- Provide a `sweepDust`-style escape (e.g., a `forceRemove` that burns/transfers a zero-value dust balance) so victims can clear slots in one call.

### Proof of Concept
Hardhat fork sketch (assumes `pool` with ≥ 2 registered `DepositToken`s `dtA`, `dtB`, …; attacker holds dust of each):

```ts
// Attacker fills victim's slots up to MAX_TOKENS_PER_USER (30)
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber();
const existing =
  (await pool.getDepositTokensOfAccount(victim.address)).length +
  (await pool.getDebtTokensOfAccount(victim.address)).length;

for (let i = 0; i < max - existing; i++) {
  const dt = depositTokens[i]; // distinct registered DepositToken
  await dt.connect(attacker).transfer(victim.address, 1); // 1 wei dust
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(max - existing);

// Victim can no longer deposit a NEW collateral type
await underlying.connect(victim).approve(dtNew.address, amount);
await expect(dtNew.connect(victim).deposit(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Victim can no longer issue a NEW synthetic
await expect(debtTokenNew.connect(victim).issue(parseEther('1'), victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Liquidation DoS variant: attacker dusts the liquidator first, then
await expect(pool.connect(liquidator).liquidate(synth, victim.address, repayAmt, dtVictim))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Note on confidence: the mechanics in `Pool.sol` and `DepositToken.sol` are verified; the per-deployment feasibility depends on how many deposit tokens are registered (countable in `deployments/*/Pool.json` histories), which was not fully enumerated here.