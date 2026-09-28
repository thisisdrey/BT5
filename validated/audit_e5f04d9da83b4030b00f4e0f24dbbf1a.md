### Title
Attacker can permanently block a victim's deposits and debt issuance by filling the victim's `MAX_TOKENS_PER_USER` token list with dust transfers - (File: contracts/Pool.sol)

### Summary
`Pool` enforces a per-account cap of `MAX_TOKENS_PER_USER = 30` across `depositTokensOfAccount + debtTokensOfAccount` via `onlyIfAdditionWillNotReachMaxTokens`. Any account can add entries to another account's list by sending dust `DepositToken` transfers (or via `seize` on liquidation). An unprivileged attacker can deposit dust into every deposit token in a pool and transfer 1 wei of each to a victim, filling the victim's list. Once full, every `addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` call for the victim reverts with `UserReachedMaxTokens`, which reverts the victim's `deposit()` of any new collateral type and `issue()`/`leverage()` of any new synthetic. Analogous to the Packetbeat finding: an attacker injects crafted "traffic" that prevents the protocol from processing the victim's legitimate activity.

### Finding Description
- `Pool.sol:79` `MAX_TOKENS_PER_USER = 30`; `Pool.sol:143-148` `onlyIfAdditionWillNotReachMaxTokens` reverts with `UserReachedMaxTokens` when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30`.
- `Pool.sol:216-220` `addToDepositTokensOfAccount(account_)` is callable only by a registered deposit token; it is invoked from `DepositToken._transfer` (`DepositToken.sol:517-520`) whenever the recipient's balance goes from 0 to >0.
- `DepositToken.transfer`/`transferFrom` (`DepositToken.sol:348-376`) are permissionless; `_revertIfLocked` only restricts the *sender's* locked collateral, not the recipient. The recipient has no way to refuse a transfer.
- Deposit tokens are freely transferable, so the attacker deposits dust (enough to receive ≥1 wei of msdTOKEN after `depositFee`) in each of the pool's deposit tokens and transfers 1 wei to the victim. Each distinct deposit token adds one entry to `depositTokensOfAccount[victim]`.
- The victim's counterattack options are limited: they can only remove an entry by fully zeroing their balance of that token (`_transfer` → `removeFromDepositTokensOfAccount` at `DepositToken.sol:523-525`), which costs a transfer per token — and the attacker can re-fill in the same block, and can also front-run the victim's removal transaction to re-add.

### Impact Explanation
While the victim's list is full:
- `DepositToken.deposit()` into any collateral the victim doesn't already hold reverts (the `_mint` → `addToDepositTokensOfAccount` path reverts), so the victim cannot add new collateral types to improve health — an unhealthy position becomes unrescuable via new collateral and can be liquidated.
- `DebtToken.issue()`/`mint()`/leverage for any synthetic the victim doesn't already hold reverts via `addToDebtTokensOfAccount`.
- Any incoming transfer of a deposit token the victim doesn't hold reverts (DoS on receipt).
This is temporary freezing of the victim's ability to use core protocol features (deposit/issue), maintained as long as the attacker keeps refilling — a direct availability/liveness break of a per-account invariant.

### Likelihood Explanation
Fully unprivileged: only requires an EOA depositing dust into each deposit token and calling `transfer(victim, 1)`. Cost is bounded by the number of deposit tokens in the pool (capped by `ReachedMaxDepositTokens`) plus gas; on cheap chains this is trivial. No governance, keeper, oracle, or malicious endpoint involvement. Modifiers (`nonReentrant`, `whenNotShutdown`, `_revertIfLocked`) do not stop the transfers.

### Recommendation
- Make token list additions opt-in or decouple them from ERC20 transfers (e.g., only add via `deposit()`/`issue()`, not `transfer`/`seize`), or
- Allow removal-free overflow handling (e.g., iterate a separate "extra" mapping), or
- Charge the recipient-side list slot to the sender's quota, or
- Let users "unregister" dust entries cheaply (already possible) but also block re-addition of tokens where the victim's balance is below a dust threshold, or add a `sweepable` minimum transfer amount.

### Proof of Concept
```ts
// Hardhat (repo's existing smock-based setup, see test/Pool.test.ts:1386)
it('fills victim token list via dust transfers -> deposits of new collaterals revert', async () => {
  const victim = bob.address;
  // attacker deposits dust into each deposit token and transfers 1 wei to victim
  for (const dt of allPoolDepositTokens) {           // >= MAX_TOKENS_PER_USER entries incl. debt tokens
    await underlying.approve(dt.address, dust);
    await dt.deposit(dust, attacker.address);        // mints msdTOKEN to attacker
    await dt.connect(attacker).transfer(victim, 1);  // DepositToken._transfer -> pool.addToDepositTokensOfAccount(victim)
  }
  expect((await pool.getDepositTokensOfAccount(victim)).length)
    .to.eq(await pool.MAX_TOKENS_PER_USER());

  // victim cannot deposit a collateral type they don't already hold
  const newDt = await smock.fake('DepositToken');
  await expect(pool.connect(newDt.wallet).addToDepositTokensOfAccount(victim))
    .revertedWithCustomError(pool, 'UserReachedMaxTokens');

  // victim cannot issue a new synthetic either
  await expect(pool.connect(debtToken.wallet).addToDebtTokensOfAccount(victim))
    .revertedWithCustomError(pool, 'UserReachedMaxTokens');
});
```
Reproduction on a fork: deploy/attach to a live Pool, loop over `pool.getDepositTokens()`, deposit `dust` into each, `transfer(victim, 1)` until length hits 30, then show `depositTokenOf[newCollateral].deposit(amount, victim)` reverts with `UserReachedMaxTokens`.