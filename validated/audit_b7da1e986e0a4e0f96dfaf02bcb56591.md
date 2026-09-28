### Title
Dust-transfer griefing fills a victim's per-account token list to `MAX_TOKENS_PER_USER`, reverting all subsequent deposits, borrows, and liquidations that add a new token - (File: contracts/Pool.sol)

### Summary
`Pool` tracks every deposit/debt token an account has touched in `depositTokensOfAccount`/`debtTokensOfAccount` (`MappedEnumerableSet`). Any addition reverts with `UserReachedMaxTokens` once the combined length reaches `MAX_TOKENS_PER_USER = 30` (`Pool.sol:143-148`). Because `DepositToken._transfer` adds the token to the *recipient's* list on any incoming transfer (`DepositToken.sol:517-520`), an attacker can permissionlessly pad a victim's list by dust-transferring deposit tokens (`msdX`), causing every later `deposit`, `issue`/borrow, `seize` (liquidation payout), `swap`-mint, or plain `transfer` that would register a new token for that account to revert. This is the Metronome analog of a null-pointer-dereference-style DoS: an attacker-controlled argument/state transition forces a guaranteed revert at the token-registration site, denying service to a victim who did nothing wrong.

### Finding Description
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance moves from 0 to >0 (`DepositToken.sol:517-520`).
- `DepositToken.transfer`/`transferFrom` only check the *sender's* unlocked balance (`DepositToken.sol:350`, `362`); nothing prevents sending dust to an arbitrary victim.
- `Pool.addToDepositTokensOfAccount` enforces `debtTokensOfAccount.length + depositTokensOfAccount.length < 30` and reverts otherwise (`Pool.sol:216-220`, `143-148`).
- After the victim's list is saturated, the following revert:
  - `DepositToken.deposit`/`_mint` for any collateral type the victim doesn't already hold (`DepositToken.sol:486-488`).
  - `DebtToken.issue`/`mint`/`flashIssue` for any synthetic the victim doesn't already owe (debt-token list add path).
  - `Pool.liquidate` → `depositToken_.seize(account_, _msgSender, _toLiquidator)` whenever the liquidator's list is saturated — attacker can dust-grief known liquidator/keeper EOAs so their `seize` reverts (`Pool.sol:589`, `DepositToken.sol:343-345`, `517-520`).
  - Any plain `transfer`/`transferFrom` to the victim of a new `msdX` or seized collateral.
- Removal only happens when a token balance returns to 0 (`DepositToken.sol:460-462`, `522-524`; `Pool.sol:630-634`), so the victim cannot register the collateral they actually want until they fully zero out attacker's dust tokens — and the attacker can immediately re-pad the freed slots.

### Impact Explanation
Denial of service / temporary freezing of funds. A victim with an unhealthy or at-risk position cannot add a *new* collateral type to recapitalize (each `deposit` reverts in `addToDepositTokensOfAccount`), cannot open new borrows, and cannot receive new deposit tokens at all — including seized collateral paid to a liquidator whose list was padded, which makes `liquidate` revert and can stall liquidation of bad debt through the dusted liquidator accounts. Recovery requires the victim to fully zero out and re-transfer each dusted token while racing the attacker, who can refill freed slots in the same block via `Operator.execute` batching. All caller-side guards (`onlyIfDepositTokenExists`, `nonReentrant`, `whenNotPaused`) are bypassed because the revert is triggered in `Pool`'s own accounting, not by attacker input validation.

### Likelihood Explanation
Cost is bounded and low: the attacker deposits 1 wei in each distinct deposit token (the pool supports at most 30 deposit tokens by `addDepositToken`'s `MAX_TOKENS_PER_USER` cap, `Pool.sol:703`), then dust-transfers to the victim. No privileged role, oracle manipulation, or capital lock-up is needed. The attack is repeatable and cheap to sustain against any address (users, liquidation bots, gateway contracts that hold `msd` tokens).

### Recommendation
- Only add to `depositTokensOfAccount` on minting paths (`deposit`/`seize` where the protocol credits the account) or gate the per-account cap on user-initiated actions — e.g., exempt `transfer`-initiated list additions from the cap or track "deposited" vs "received" separately.
- Alternatively, let `addToDepositTokensOfAccount` fail-open for transfer recipients (emit without reverting) and only enforce `MAX_TOKENS_PER_USER` on `deposit`/`issue`.
- Consider a minimum-transfer threshold or allowing accounts to self-remove unwanted entries.

### Proof of Concept
Hardhat fork-style PoC against `test/` harness (e.g., extend `VesperGateway.test.ts`/`DepositToken` fixtures):

```ts
// Setup: pool with deposit tokens msd[0..29] registered, victim = alice
for (const msd of depositTokens) {
  // attacker deposits dust and sends it to the victim
  await underlying(msd).approve(msd.address, 1n)
  await msd.deposit(1n, attacker.address)          // mint 1 wei msd
  await msd.transfer(victim.address, 1n)           // adds msd to victim's list
}
// victim list is now at MAX_TOKENS_PER_USER (30)

// 1) Victim cannot deposit a collateral type they don't hold
await expect(msdNew.connect(victim).deposit(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 2) Victim cannot issue a new synthetic (debt-token list add reverts)
await expect(debtTokenNew.connect(victim).issue(amount, victim.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 3) Liquidation via a dusted liquidator reverts on seize
//    (pad liquidator.address list first, then:)
await expect(pool.connect(liquidator).liquidate(msAsset, victim.address, repayAmt, msdCollateral))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Each transfer into a zero-balance account hits `DepositToken._transfer` → `pool.addToDepositTokensOfAccount(recipient)` (`DepositToken.sol:517-520`), and once `length == 30` every subsequent add reverts via `onlyIfAdditionWillNotReachMaxTokens` (`Pool.sol:143-148`), reproducing the DoS deterministically.