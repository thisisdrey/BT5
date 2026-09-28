### Title
Dust-transfer griefing of `depositTokensOfAccount` blocks collateral top-ups and causes forced liquidations - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The PoDoFo CVE is a stack buffer overflow causing a crash/DoS via an attacker-controlled write past a bound. The Solidity analog in Metronome is a bounded-but-attacker-fillable write: `Pool.addToDepositTokensOfAccount` appends to a per-account array capped at `MAX_TOKENS_PER_USER = 30`, and it is invoked internally on every `DepositToken._transfer`/`_mint` when the recipient's balance moves from zero to nonzero. An attacker can push dust of every listed `DepositToken` to a victim so the combined `debtTokensOfAccount + depositTokensOfAccount` length reaches 30, after which every mint/transfer of a *new* token type to that victim reverts with `UserReachedMaxTokens`, including the victim's own `deposit()` of a new collateral type.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`Pool.sol:143-148`).
- `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient previously held zero (`DepositToken.sol:517-520`). There is no opt-out: a recipient cannot refuse inbound deposit tokens.
- `DepositToken.transfer`/`transferFrom` only check the *sender's* unlocked balance (`DepositToken.sol:348-376`), so the attacker only needs dust balances of each listed deposit token, obtainable cheaply by calling `deposit()` once per underlying.
- After the victim's list is full:
  - `deposit()` of any new collateral type reverts in `_mint → addToDepositTokensOfAccount` (`DepositToken.sol:486-488`), so the victim cannot add a *different* collateral to rescue an unhealthy position.
  - Any `transfer`/`transferFrom`/`seize` of a new token type *to* the victim reverts.
  - `SmartFarmingManager.leverage`, which mints a chosen deposit token to the user, reverts if that token type is not already in the victim's list.
- The victim can only recover by fully emptying one of their token positions (`_burn`/`_transfer` removes the token when the balance hits zero, `DepositToken.sol:460-462, 522-525`), which may itself be impossible while the position is near liquidation because `withdraw` is gated by `unlockedBalanceOf`/`_revertIfLocked` (`DepositToken.sol:383-398, 406-411`).

### Impact Explanation
Temporary freezing of funds with a path to indirect loss. A victim holding debt is locked in exactly the scenario where they need to act: when their collateral factor drops, adding a *new* collateral type reverts, freeing that slot requires withdrawing to zero (blocked by `_revertIfLocked` once unhealthy), so the position becomes liquidatable and the victim eats the liquidation penalty/bad debt. Receiving deposit tokens (e.g., OTC transfers of collateral) is also bricked. The invariant broken is liveness of `deposit()`/inbound transfers for the victim, controlled purely by an unprivileged attacker.

### Likelihood Explanation
Attack requires only an EOA, dust capital, and a pool whose combined registered `depositTokens + debtTokens` count approaches 30 (both lists count toward the cap; a borrower's existing debt tokens reduce the dust needed). Each dust `transfer` costs only gas plus a 1-wei-scale deposit. Attack must be timed/held until the victim needs a new collateral type, but the dust itself persists on the victim's list at zero cost to the attacker (attacker retains the bulk of each balance and can reclaim dust later). No privileged role, oracle manipulation, or external dependency is involved; `nonReentrant`, pause flags, and SynthContext sender checks do not mitigate it since `transfer` is an ordinary public entry point.

### Recommendation
- Make set membership revert-free for the *recipient*: cap enforcement should apply to the party creating the position, not the passive receiver. E.g., move the `onlyIfAdditionWillNotReachMaxTokens` check out of `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` and into user-initiated entry points (`deposit`, `issue`/`mint` on DebtToken), or skip `add` when the transfer is unsolicited and index positions by iterating all listed tokens instead.
- Alternatively, allow `seize`/liquidation paths to bypass the cap, and let recipients remove unwanted tokens via a zero-amount `withdraw`-style cleanup.

### Proof of Concept
Hardhat (existing test scaffolding in `test/Pool.test.ts` already fakes Deposit/Debt tokens):

```ts
it('griefs victim: fills depositTokensOfAccount to MAX and bricks new deposits', async () => {
  const victim = alice.address;
  const max = (await pool.MAX_TOKENS_PER_USER()).toNumber(); // 30

  // Victim already holds k debt tokens (normal borrowing activity)
  // Attacker deposits dust in each listed deposit token and transfers 1 wei to victim
  for (let i = 0; i < max; ++i) {
    const dt = depositTokens[i]; // pool-listed DepositToken
    await underlying[i].connect(attacker).approve(dt.address, 1);
    await dt.connect(attacker).deposit(1, attacker.address);
    await dt.connect(attacker).transfer(victim, 1); // adds to victim's list
  }
  expect(await pool.getDepositTokensOfAccount(victim)).length(max);

  // Victim tries to add NEW collateral to rescue position -> reverts
  await expect(
    newDepositToken.connect(victim).deposit(amount, victim)
  ).revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // Inbound transfer of any new deposit token to victim also reverts
  await expect(
    newDepositToken.connect(attacker).transfer(victim, 1)
  ).revertedWithCustomError(pool, 'UserReachedMaxTokens');
});
```

Caveat: validity depends on the deployed pool having enough listed deposit/debt tokens for the attacker to reach 30 on the victim's combined list; pools with few assets make the attack weaker or require the victim to already hold several token types.