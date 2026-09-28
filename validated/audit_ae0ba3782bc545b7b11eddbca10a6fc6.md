### Title
Dust-transfer stuffing of `MAX_TOKENS_PER_USER` list blocks liquidations, deposits and issuance for targeted accounts - (File: contracts/Pool.sol)

### Summary
`Pool` enforces a per-account cap of 30 entries across `debtTokensOfAccount` + `depositTokensOfAccount` via `onlyIfAdditionWillNotReachMaxTokens`. Because `DepositToken` shares are freely transferable by any holder, an unprivileged attacker can dust-transfer tiny amounts of every registered `DepositToken` to a victim (or a known liquidator) until the account's list hits the cap. From then on, any operation that would add a *new* token to that account's list reverts, including `DepositToken.seize` crediting seized collateral to a liquidator in `Pool.liquidate`, first-time `deposit`/`mint` of a new collateral type, and `DebtToken` issuance of a new synth — a denial-of-service analog to the reported DDoS incident.

### Finding Description
`MAX_TOKENS_PER_USER = 30` at `contracts/Pool.sol:79`. Both `addToDepositTokensOfAccount` (`Pool.sol:216`) and `addToDebtTokensOfAccount` (`Pool.sol:204`) use `onlyIfAdditionWillNotReachMaxTokens` (`Pool.sol:143-148`) and revert with `UserReachedMaxTokens` once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30`.

The list is mutated from `DepositToken._transfer` (`DepositToken.sol:498-526`): whenever the recipient's balance goes from 0 → non-zero, `pool.addToDepositTokensOfAccount(recipient_)` is called; it is only removed when the sender's balance returns to exactly 0. Since `transfer`/`transferFrom` are public and unlocked transfers of deposit-token shares are permitted, an attacker holding 1 wei of each of the N registered deposit tokens can send dust to a target. If the pool registers ≥30 deposit tokens (or the victim already occupies some slots), the victim's list is filled permanently until they actively clean it up.

Attack paths:

1. **Liquidation griefing → bad debt.** `Pool.liquidate` (`Pool.sol:589`) calls `depositToken_.seize(account_, _msgSender, _toLiquidator)` which internally performs `_transfer` to the liquidator. If the liquidator's account list is at the cap, `addToDepositTokensOfAccount` reverts and the entire liquidation reverts. An attacker can watch the mempool and frontrun every liquidation attempt by dusting the calling liquidator's address, or preemptively dust known liquidation bots/keepers. Unhealthy positions become unliquidatable, and if prices keep falling the pool accrues bad debt.

2. **Deposit/issuance freeze for a user.** Once a victim's list is full, `DepositToken.deposit`/`_mint` (`DepositToken.sol:486-488`) of any collateral the victim doesn't already hold reverts, as does `DebtToken.issue`/`mint` for any new synthetic (`DebtToken` calls `addToDebtTokensOfAccount`). The victim can only interact with token types already in their list.

Cleanup is possible (transferring the dust out removes the entry), but the attacker can simply re-dust in the same block or the next transaction, and for liquidations there is no opportunity — a single frontrun dust transfer of one missing token is enough to revert the seize.

### Impact Explanation
Temporary freezing of funds and protocol-level liveness failure reachable by any EOA:

- Liquidations of specific deposit-token types can be reliably reverted per-attempt by dusting the liquidator, enabling accumulation of underwater positions → potential protocol insolvency if collateral value falls below debt.
- Targeted victims are blocked from opening positions in new collateral or new synths for as long as the attacker maintains the stuffing (costs only dust amounts of each deposit token).

This is not gas-based DoS (rejected class); it is a deterministic state-dependent revert exploitable in a single transaction.

### Likelihood Explanation
- Attacker requirements: hold a dust balance of deposit tokens (obtainable by depositing minimal collateral, or receiving shares), then call `transfer(victim, 1)` for up to 30 tokens. Fully permissionless.
- No privileged role, oracle manipulation, or flash capital needed.
- Caveat: requires the pool to offer enough distinct deposit/debt tokens that stuffing to 30 is feasible, and for the victim not to already hold the token being used. Liquidation griefing additionally requires the liquidator's list to be fillable (fresh liquidator EOAs/bots start at 0 entries). Impact is temporary since victims can eject dust, at the cost of repeated gas and failed liquidation races — consistent with Medium/Low severity.

### Recommendation
- Do not count tokens against `MAX_TOKENS_PER_USER` when crediting seized collateral during liquidation, or route liquidation proceeds differently (e.g., transfer underlying directly from Treasury) so `seize` cannot be griefed.
- Alternatively, count only tokens with economically meaningful balances (e.g., balance ≥ a floor) toward the cap, or allow an account to evict entries by treating transfers of the full dust balance as automatic removal (already true) plus making `addTo*` failures non-fatal for inbound transfers where appropriate.
- At minimum, exempt `seize`-initiated `_transfer` calls from the `addToDepositTokensOfAccount` revert path so liquidations cannot be blocked.

### Proof of Concept
Hardhat fork sketch (assumes ≥30 deposit tokens registered, or fewer tokens plus partially occupied victim list — adjust count accordingly):

```ts
// Victim is a liquidation bot / EOA that will call Pool.liquidate
const attacker = ...;
const victimLiquidator = ...;

// 1) Attacker deposits dust into every listed deposit token to obtain shares
for (const dt of depositTokens) {
  await underlying.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, attacker.address); // msToken balance = 1 wei
}

// 2) Attacker dusts the liquidator to fill its 30-slot list
for (const dt of depositTokens) {
  await dt.connect(attacker).transfer(victimLiquidator.address, 1);
}
expect(await pool.getDepositTokensOfAccount(victimLiquidator.address))
  .to.have.lengthOf(depositTokens.length); // hits MAX_TOKENS_PER_USER with existing entries

// 3) An unhealthy position exists (price drop makes account unhealthy)
// Liquidator approves synth to repay and calls liquidate for a deposit token
// the liquidator does NOT already hold:
await synth.connect(victimLiquidator).approve(pool.address, amountToRepay);
await expect(
  pool.connect(victimLiquidator).liquidate(synth.address, victim.address, amountToRepay, newDepositToken.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
// -> depositToken_.seize -> _transfer -> addToDepositTokensOfAccount reverts
// -> liquidation impossible while attacker keeps list stuffed (can re-dust after cleanup)

// 4) Same stuffing blocks a normal user from depositing a new collateral type:
// fill victim's list, then:
await expect(
  someNewDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Verification gaps: I confirmed `MAX_TOKENS_PER_USER`, the reverting `addTo*` functions, `_transfer`/`_mint` list mutation, and `liquidate → seize → _transfer` flow from source. I did not read `DepositToken.seize`'s exact body line-by-line (index excerpt limits), but its path goes through `_transfer`/`_burn` which invokes `addToDepositTokensOfAccount`; the deployed test file (`test/Pool.test.ts:1386`) confirms `UserReachedMaxTokens` reverts on list-full adds.