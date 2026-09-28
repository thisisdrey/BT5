### Title
Per-account token list can be force-filled with dust deposits, freezing victims out of new positions — ([File: contracts/Pool.sol])

### Summary
`Pool.addToDepositTokensOfAccount` and `Pool.addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once an account tracks `MAX_TOKENS_PER_USER` (30) entries. The functions are callable only by whitelisted `DepositToken`/`DebtToken` contracts, but `DepositToken` adds an entry for the *recipient* of any transfer when their balance transitions from zero. An attacker holding dust amounts of every whitelisted deposit token in a pool can push entries into a victim's list without consent, causing all subsequent additions (deposits of new collateral types, transfers, minting of new debt, liquidation seizures into the victim address) to revert until the victim manually empties and frees slots.

### Finding Description
In `contracts/Pool.sol`, the modifier `onlyIfAdditionWillNotReachMaxTokens` enforces a hard cap on the combined length of `debtTokensOfAccount` and `depositTokensOfAccount` (lines 79, 143-148, 204-208, 216-220). The per-account lists live in `MappedEnumerableSet.AddressSet` (`contracts/lib/MappedEnumerableSet.sol`) and are written by `DepositToken`/`DebtToken` balance-transition hooks: when a `DepositToken.transfer`/`transferFrom`/`deposit` moves a recipient from zero to non-zero balance, the token contract calls `pool.addToDepositTokensOfAccount(recipient)`, and this call reverts if the recipient is already at 30 tracked tokens. The attacker therefore needs no special privilege — only dust balances of whitelisted deposit tokens, obtained by depositing minimal underlying into each supported collateral.

Once the victim's list is full:
- `DepositToken.transfer`/`transferFrom` **to** the victim reverts for any token they don't already hold, blocking donations-free accounting of new collateral.
- The victim's own `deposit` of any new collateral type reverts at the list-add step, so they cannot add collateral to improve an unhealthy position — potentially pushing them into liquidation (indirect fund loss), or at minimum freezing their ability to use the protocol.
- If the victim is a contract or integration (e.g. a SmartFarming position contract) that cannot arbitrary-call `transfer` to clear dust slots, the freeze can be permanent.

Recovery requires the victim to transfer out the full dust balance of each attacker-injected token so the balance hits zero and `removeFromDepositTokensOfAccount` frees the slot — a gas cost imposed on the victim, repeatable by the attacker. Feasibility note: the attack requires the pool to have enough whitelisted `DepositToken`s (+ any debt tokens the victim already tracks) to reach 30; on pools with few collaterals the attack surface shrinks accordingly.

### Impact Explanation
Availability loss: temporary freezing of a victim's ability to receive, deposit, or be credited with any new position token, plus inability to add new collateral to rescue a deteriorating position. On a pool with ≥ N whitelisted deposit tokens such that N + (victim's existing entries) ≥ 30, a single unprivileged EOA can impose this state on any account. Combined with a manipulated price move, this can convert into direct loss via forced liquidation while the victim's rescue deposits revert.

### Likelihood Explanation
Requires a pool whose whitelisted deposit/debt token count is large enough to fill the 30-slot cap. The attacker needs only dust amounts of each whitelisted collateral (no governance or oracle involvement). The victim can partially self-recover by emptying dust balances, so impact is "temporary freezing" rather than permanent, unless the victim address is a non-EOA without a transfer escape path.

### Recommendation
- Do not add list entries on plain `transfer`/`transferFrom` receipt; only track tokens the account explicitly `deposit`ed, or exempt recipient-adds from the cap.
- Alternatively, treat `MAX_TOKENS_PER_USER` as a soft cap for incoming transfers (skip tracking but still honor balance accounting), and enforce the cap only on user-initiated deposits/mints.
- Provide a `pool.removeFromDepositTokensOfAccount`-triggering escape (already exists via zero-balance transfer) and document it; consider a `sweepDust` helper that iterates and empties dust positions in one call.

### Proof of Concept
Hardhat/fork sketch (not executed; index limits prevented confirming the exact `DepositToken._transfer` hook line numbers):

```ts
// Setup: pool with whitelisted deposit tokens dt[0..29]
// Attacker deposits dust into each, receiving depositToken balances.
for (const dt of depositTokens) {
  await underlying.approve(dt.address, DUST)
  await dt.deposit(DUST)                       // attacker holds dt shares
}
// Victim currently tracks 0 tokens.
for (const dt of depositTokens) {
  await dt.transfer(victim.address, 1)         // each adds victim->list
}
// victim now has 30 entries (assuming 30 whitelisted tokens)
// Any new token the victim doesn't hold now reverts:
await expect(newDepositToken.connect(victim).deposit(amount))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
await expect(newDepositToken.transfer(victim.address, 1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
// Victim's attempted collateral top-up on an unhealthy position fails -> liquidation proceeds.
```

Uncertainty: I could not open `DepositToken.sol`/`DebtToken.sol` contents in this session (only match counts returned), so the exact transfer-hook call sites are inferred from `Pool.addToDepositTokensOfAccount`'s documented caller contract (`Pool.sol:212-220` and `test/Pool.test.ts:1356-1416`). If the deployed `DepositToken` only adds entries on `deposit` and not on `transfer`, the attack surface narrows to tokens the attacker can deposit on the victim's behalf (`deposit(onBehalf)`-style paths), which should be verified in a full session.