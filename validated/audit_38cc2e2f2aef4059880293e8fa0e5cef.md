### Title
Dust DepositToken transfers fill a victim's `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER`, blocking them from depositing new collateral types while they hold locked debt - (`contracts/DepositToken.sol` / `contracts/Pool.sol`)

### Summary
An unprivileged attacker can grief any account that has an open debt position by depositing dust amounts of every whitelisted collateral and transferring the resulting `msdTOKEN`s to the victim. Each transfer calls `Pool.addToDepositTokensOfAccount(victim)` until the victim's combined `depositTokensOfAccount` + `debtTokensOfAccount` length hits `MAX_TOKENS_PER_USER` (30). From then on, any `deposit()` or token receipt of a *new* collateral type reverts with `UserReachedMaxTokens`. Because the victim's balance is locked (`_revertIfLocked` → `unlockedBalanceOf` returns ~0 while `debtInUsd > 0` and `_issuableInUsd == 0`), the victim cannot transfer the dust away to free a slot. This is an attacker-imposed denial of service on the victim's position management — the direct analog of CVE-2018-2782's low-privilege remote availability (hang/crash) impact.

### Finding Description
- `DepositToken._transfer` adds the token to the recipient's per-account list whenever `balanceOf[recipient] == 0` (`contracts/DepositToken.sol:518-520`) and only removes it when the *sender's* balance hits zero. There is no opt-out for the recipient.
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts with `UserReachedMaxTokens` once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:79,143-148`); `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` enforce it (confirmed by `test/Pool.test.ts:1386-1416`).
- Removal is only possible if the victim can transfer/withdraw, but `transfer`, `transferFrom`, and `withdraw` all call `_revertIfLocked` (`contracts/DepositToken.sol:180-182,350,362,409`), and `unlockedBalanceOf` returns 0 for an account with debt and no remaining issuable headroom (`contracts/DepositToken.sol:383-398`).

Attack path:
1. Victim holds collateral A and an outstanding debt, with little/no `_issuableInUsd` headroom (e.g., a position drifting toward liquidation).
2. Attacker, for each whitelisted `DepositToken` the victim doesn't yet hold, calls `deposit(1 wei, attacker)` then `msdTOKEN.transfer(victim, 1)`. Each call pushes one entry into `depositTokensOfAccount[victim]` until it reaches 30.
3. Victim's `deposit(newCollateral)` now reverts (`_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`), and victim cannot shed the dust because all balances are locked by `_revertIfLocked`.

### Impact Explanation
The victim is denied the ability to add new collateral to an at-risk position and cannot receive airdropped/new deposit tokens at all. If the position's health deteriorates, liquidation becomes unavoidable because the rescue path (`deposit` of a different collateral) is bricked — temporary freezing of funds and forced liquidation. Cost to attacker is only gas plus dust amounts of each underlying (recoverable).

### Likelihood Explanation
Requires only an EOA and public entry points (`deposit`, `transfer`); no privileged role, oracle manipulation, or governance action. Preconditions: victim has debt with low headroom, and the pool has enough distinct collateral tokens (combined with the victim's existing debt tokens) to reach 30 slots — plausible on mainnet pools with many deposit/debt tokens. Severity is bounded (per-account, temporary until debt is repaid or a slot frees), consistent with a medium-severity availability analog.

### Recommendation
- Let recipients prune their own list: allow `removeFromDepositTokensOfAccount` semantics on zero-balance, or let a user call a `sweepTokenFromAccount(token)` that removes entries with `balanceOf == 0` (note: dust has nonzero balance, so also allow burning dust without lock checks, or exempt list-maintenance reverts for recipients).
- Alternative: skip `addToDepositTokensOfAccount` on plain `transfer`/`transferFrom` (only register on `deposit`/`seize`), or make the cap check apply to the *depositor* rather than silently blocking unsolicited recipients.

### Proof of Concept
Hardhat sketch (against existing test fixtures in `test/Pool.test.ts`):

```ts
it('griefs victim token list', async () => {
  // victim deposits collateral and mints debt so issuable headroom ~0
  await depositTokenA.connect(victim).deposit(amount, victim.address)
  await debtToken.connect(victim).issue(maxIssuable, victim.address)

  // attacker fills remaining slots with dust msdTOKENs
  for (const dt of otherDepositTokens) {
    await underlying.connect(attacker).approve(dt.address, 1)
    await dt.connect(attacker).deposit(1, attacker.address)
    await dt.connect(attacker).transfer(victim.address, 1) // adds to victim's list
  }
  // victim's list is now at MAX_TOKENS_PER_USER

  // victim cannot remove dust: balances locked by _revertIfLocked
  await expect(
    otherDepositTokens[0].connect(victim).transfer(attacker.address, 1)
  ).to.be.revertedWithCustomError(otherDepositTokens[0], 'NotEnoughFreeBalance')

  // victim cannot deposit a new collateral type
  await expect(
    newDepositToken.connect(victim).deposit(1, victim.address)
  ).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
})
```

Note: this draft assumes `deposit(1)` mints a nonzero balance to the attacker; if `depositFee` rounds the dust to zero, the attacker seeds slightly larger amounts per token. The mechanism (`_transfer` unconditionally registering recipients, cap enforced on `addTo*`, locked balances blocking cleanup) is verified in `DepositToken.sol:518-525`, `Pool.sol:143-148`, and `DepositToken.sol:180-182,383-398`. I could not execute the test — verification of exact revert ordering on a live fork should be confirmed during remediation.