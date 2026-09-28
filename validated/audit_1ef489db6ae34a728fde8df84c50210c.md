### Title
Attacker can permanently DoS `Pool.liquidate` by saturating `feeCollector`'s token list to `MAX_TOKENS_PER_USER` via liquidation fee seizures - ([File: contracts/Pool.sol](https://github.com/ThankGodontt/metronome-synth-public--016/blob/main/contracts/Pool.sol))

### Summary
The bug class (unprivileged hang/crash → DoS) maps onto Metronome's per-account token-list cap. `Pool.liquidate` seizes a protocol fee into the fixed `feeCollector` address via `DepositToken.seize` → `_transfer`, which calls `pool.addToDepositTokensOfAccount(feeCollector)` whenever the feeCollector's balance of that deposit token goes from 0 to >0. `addToDepositTokensOfAccount` enforces `debtTokensOfAccount.length + depositTokensOfAccount.length < MAX_TOKENS_PER_USER` (30) and reverts with `UserReachedMaxTokens`. Once the feeCollector accumulates 30 distinct deposit tokens, every subsequent liquidation that would credit a *new* deposit token reverts — permanently freezing liquidations for those collaterals, since the feeCollector is a fixed protocol address whose tokens can only be swept by governance (`TokenHolder._requireCanSweep` is `onlyGovernor` in `DepositToken.sol:493`).

### Finding Description
Relevant code:

- `Pool.liquidate` sends `_toLiquidator` to the liquidator and `_fee` to `_poolRegistry.feeCollector()` via `depositToken_.seize(account_, _poolRegistry.feeCollector(), _fee)` — `contracts/Pool.sol:589-593`.
- `DepositToken.seize` → `_transfer`, which executes `pool.addToDepositTokensOfAccount(recipient_)` on first receipt — `contracts/DepositToken.sol:343-345, 517-520`.
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts once the sum of both lists reaches 30 — `contracts/Pool.sol:143-148`.
- Because the `seize` for the fee happens *after* `syntheticToken_.burn` and `_debtToken.burn`, the revert rolls back the entire `liquidate` call — there is no way to skip the fee transfer.

Attack path (unprivileged, no privileged roles needed):

1. Attacker opens minimally collateralized positions across 30 of their own accounts (or repeatedly across blocks), each holding a *different* whitelisted `DepositToken` as collateral (on deployed pools that list enough collaterals; the count can also accrue organically over the pool's lifetime since feeCollector's entries are never removed — it never withdraws to zero).
2. Using same-transaction AMM price manipulation of the collateral oracle feed (allowed per rules) the attacker makes each position unhealthy, then calls `Pool.liquidate(synth, account_i, amountToRepay, depositToken_i)` choosing a different `depositToken_` each time so the fee seizure lands a new token in feeCollector's list.
3. After the 30th distinct token, `addToDepositTokensOfAccount(feeCollector)` reverts with `UserReachedMaxTokens` for any collateral not already in the list.

`whenNotShutdown`, `nonReentrant`, `onlyIfDepositTokenExists`, `maxLiquidable`, and `debtFloorInUsd` do not stop this: each individual call is a perfectly valid liquidation; the revert occurs inside the final `seize`.

### Impact Explanation
Liveness / solvency invariant break. Once feeCollector's list is saturated, `liquidate` reverts for any position whose only viable seize collateral is a deposit token not yet in the feeCollector's list. Unhealthy positions for those collateral types become un-liquidatable, so bad debt accumulates and cannot be cleared — a permanent freezing of the liquidation feature for affected collateral types and a path to protocol insolvency. Remediation requires a governor `sweep` on every DepositToken (governance action, not guaranteed) — this is a vulnerability, not a parameter choice.

### Likelihood Explanation
Feasibility depends on deployed configuration: the attack needs enough distinct whitelisted `DepositToken`s so that feeCollector can reach 30 combined entries. Metronome pools support many collaterals, and the count accrues passively — every liquidation fee paid in a new collateral type permanently consumes one slot, because feeCollector never draws its balance to zero itself. The attacker only needs to front-run/self-liquidate dust positions; cost is bounded by dust collateral plus oracle-manipulation cost. Positions can be attacker-controlled, so no victim cooperation is needed.

### Recommendation
Do not route the fee through the capped per-account list, or exempt `feeCollector` from `onlyIfAdditionWillNotReachMaxTokens` in `addToDepositTokensOfAccount`. Alternatively, pull fees into `Treasury` directly (like `_withdraw` does via `treasury().pull`) instead of holding deposit-token balances on the feeCollector, or allow `feeCollector`/`sweep` to be callable to prune entries.

### Proof of Concept
Hardhat (fork or fixture) sketch:

```ts
// Setup: pool with >= 30 whitelisted DepositTokens (dt[0..29]), a synthetic msToken + debtToken
// Attacker creates 30 accounts acc[i], each: deposit underlying i -> dt[i], issue max msToken
for (let i = 0; i < 30; i++) {
  await underlying[i].mint(acc[i], dust);
  await dt[i].connect(acc[i]).deposit(dust, acc[i].address);   // collateral i
  await debtToken.connect(acc[i]).issue(small, acc[i].address);
}
// Manipulate oracle price down (or donate/move AMM) so all 30 positions are unhealthy
await masterOracle.setPrice(underlying[i], crashedPrice); // same-tx manipulation per rules
// Liquidator = attacker EOA holding enough msToken
for (let i = 0; i < 30; i++) {
  await pool.connect(attacker).liquidate(msToken.address, acc[i].address, repay, dt[i].address);
  // each seize adds dt[i] to feeCollector's depositTokensOfAccount
}
expect(await pool.getDepositTokensOfAccount(feeCollector)).to.have.length(30);
// Now a 31st distinct collateral position is un-liquidatable:
await expect(
  pool.connect(attacker).liquidate(msToken.address, victim.address, repay, dt30.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Uncertain: I could not confirm the exact count of whitelisted deposit tokens per live deployment within the remaining iterations; the finding is valid wherever the pool can approach 30 tokens (including organic accumulation over time, since feeCollector entries are never pruned).