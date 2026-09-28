### Title
Griefing via dust transfers fills victim's `MAX_TOKENS_PER_USER` slots, blocking deposits and collateral top-ups — ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`DepositToken._transfer` adds a deposit token to the recipient's per-account token list on first receipt, with no opt-in or minimum amount. `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts once an account holds `MAX_TOKENS_PER_USER = 30` entries across `debtTokensOfAccount` + `depositTokensOfAccount`. An unprivileged attacker can dust-transfer each deposit token to a victim, exhausting the victim's slots. This is the Metronome analog of CVE-2018-1064's bug class: an untrusted party forcing consumption of a bounded per-account resource (the 30-slot set) until dependent operations (deposits, incoming transfers, `deposit onBehalfOf`) start reverting.

### Finding Description
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever `_recipientBalanceBefore == 0 && amount_ > 0` (DepositToken.sol:518-520). The recipient has no way to refuse the inbound token.
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts with `UserReachedMaxTokens` when `debtTokensOfAccount + depositTokensOfAccount >= 30` (Pool.sol:143-148).
- Consequently, once an attacker dust-fills a victim's list to 30 entries:
  - `DepositToken.deposit(amount_, onBehalfOf_ = victim)` reverts, because `_mint` → `addToDepositTokensOfAccount` (DepositToken.sol:486-488).
  - `transfer` / `transferFrom` / `seize` of any *new* deposit token to the victim reverts at `_transfer`.
  - `SmartFarmingManager.leverage` / `flashRepay` flows that mint or transfer a new deposit token to the victim revert.
- The victim's existing collateral remains withdrawable only via unlocked amounts, but critically, a victim approaching liquidation cannot deposit a *new* collateral type to restore health — every rescue path reverts. Existing collateral top-ups for tokens already in the list still work (balanceBefore != 0), so the attack is most damaging to users holding fewer than all listed collaterals.
- Note `DepositToken.deposit` does not apply `nonReentrant`? It does apply `nonReentrant` and `whenNotPaused`; none of these stop the attack, which uses ordinary `transfer`.

### Impact Explanation
Temporary freezing of funds / forced liquidation. An unprivileged EOA can, at the cost of ~30 dust transfers, prevent a victim from receiving any additional collateral token or deposit token balance additions. A victim whose position becomes unhealthy during the grief window cannot add new collateral to avoid liquidation, suffering avoidable liquidation losses. The condition persists until the victim manually empties slots (transferring positions elsewhere), which itself requires the dusted tokens to reach zero balance — a recovery step the attacker does not pay for.

### Likelihood Explanation
- Fully permissionless: requires only holding dust amounts of each `DepositToken` and calling `transfer(victim, 1)` — reachable directly or via `Operator.execute`.
- Cost is minimal (the dust stays recoverable by the attacker since entries aren't consumed, only per-account entries for the victim are created).
- No governor/guardian/oracle involvement; no oracle manipulation, no gas/unbounded-loop DoS — the exhaustion target is a fixed storage-slot cap, not a loop bound.
- Severity is bounded by the victim's ability to eventually self-clean the list, so the correct classification is temporary freezing of funds rather than permanent loss.

### Recommendation
- Make the recipient opt in before occupying a slot: e.g., only add to `depositTokensOfAccount` inside `deposit`/`_mint`, and keep a separate counter for transfer-received tokens, or
- Track "registered" tokens separately from "received dust" (e.g., require `amount_ >= MIN_DUST` or an explicit per-account allowlist), or
- Let `removeFromDepositTokensOfAccount` be callable by the account itself so victims can evict dust entries without liquidating positions, or
- Raise `MAX_TOKENS_PER_USER` beyond the number of registered deposit tokens and bound the number of *registered* tokens at the pool level.

### Proof of Concept
Hardhat sketch (fork or local deployment fixture):

```ts
// given: pool with >= 2 deposit tokens msdA, msdB, ... and MAX_TOKENS_PER_USER = 30
// attacker holds 1 wei of msdA..msdN
for (const dt of depositTokens) {
  await dt.connect(attacker).transfer(victim.address, 1); // adds dt to victim's list
}
// victim's list now has N entries; attacker repeats across all listed deposit tokens
// until depositTokensOfAccount(victim).length + debtTokensOfAccount(victim).length == 30

// when: victim (or anyone) tries to deposit a NEW collateral type on victim's behalf
await expect(
  msdNew.connect(anyone).deposit(parseEther('1'), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// also: transfer of a new deposit token to victim reverts
await expect(
  msdNew.connect(holder).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// then: if victim's position turns unhealthy, top-up with a new collateral type is impossible
// -> liquidation proceeds that a healthy deposit would have prevented
```

Caveat: I confirmed the add-on-first-receipt logic in `DepositToken._transfer` (lines 518-520), the cap modifier in `Pool` (lines 143-148), and the revert behavior in `test/Pool.test.ts:1386-1416`. I did not read `Pool.addToDepositTokensOfAccount` directly to confirm the modifier is applied on the token-initiated path, though the test confirms the revert propagates through `addToDepositTokensOfAccount`.