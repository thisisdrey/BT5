### Title
Dust-transfer griefing fills victim's token list and blocks deposits, borrows, and leverage (DoS) - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` tracks per-account deposit and debt tokens in `MappedEnumerableSet` lists capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79,143-148`). The cap is enforced inside `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` via `onlyIfAdditionWillNotReachMaxTokens` (`contracts/Pool.sol:204,216`), which reverts with `UserReachedMaxTokens` once the combined length reaches 30. Crucially, an entry is added for the *recipient* of any `DepositToken._transfer` whenever the recipient's prior balance is zero (`contracts/DepositToken.sol:517-520`), and likewise for `_mint` (`contracts/DepositToken.sol:485-488`), which is reached by the public `deposit(amount_, onBehalfOf_)` (`contracts/DepositToken.sol:211-234`). Neither path requires the recipient's consent. An unprivileged attacker can therefore push dust of every whitelisted deposit token into a victim's account (and, once `DebtToken` transfers are available or via `issue`, debt-token entries too) until the victim hits 30 entries, after which any new deposit/mint to the victim reverts.

### Finding Description
- `DepositToken.transfer`/`transferFrom` only check the *sender's* unlocked balance via `_revertIfLocked` (`contracts/DepositToken.sol:348-375`) — the recipient's list state is never consulted.
- `_transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` when `recipientBalanceBefore == 0` (`contracts/DepositToken.sol:518-520`), and `Pool.addToDepositTokensOfAccount` reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= MAX_TOKENS_PER_USER` (`contracts/Pool.sol:143-148,216-220`).
- `deposit(amount_, onBehalfOf_)` accepts an arbitrary beneficiary (`contracts/DepositToken.sol:216`) and mints to them via `_mint`, which also calls `addToDepositTokensOfAccount` (`contracts/DepositToken.sol:486-488`). Both vectors let an attacker add list entries to any account with only dust cost.
- Once the victim's list is full, every subsequent `deposit`/`transfer`/`seize`/`issue` that would add a *new* token reverts in `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount`, so the transaction fails.

Attack: attacker deposits/atomically transfers 1 wei of each whitelisted deposit token to victim. With `ReachedMaxDepositTokens` pools typically having <30 collateral types, attacker combines deposit-token dust with induced debt-token entries (e.g., via `SmartFarmingManager.leverage`-style flows or by paying dust into positions) until the victim has 30 entries. The victim then cannot deposit a new collateral type, cannot mint a new synthetic, cannot `Pool.swap` into a new synthetic position, and cannot be `leverage`d — all revert with `UserReachedMaxTokens`. Removal only happens when a balance reaches exactly 0 (`_burn`/`_transfer` remove on `balanceOf == 0`, `contracts/DepositToken.sol:460-462,523-525`), but dust deposits count toward collateral and the locked portion (`unlockedBalanceOf` at `contracts/DepositToken.sol:383-398`) cannot be transferred out while the victim has debt, so cleanup may require first repaying debt or forfeiting the dust via `withdraw`.

### Impact Explanation
Denial of service against a targeted user: the victim is blocked from adding new collateral types or issuing new synthetics, and integrations (gateways, `SmartFarmingManager.leverage`, liquidation `seize` to a saturated liquidator) revert. Funds are not stolen, but position management is degraded — e.g., a victim cannot top up a new collateral type to improve health, and recovery requires gas-intensive slot cleanup that may be impossible while dust is locked against existing debt. This matches the "temporary freezing / liveness DoS" bug class of CVE-2022-20796.

### Likelihood Explanation
Requires only an unprivileged EOA, public entry points (`DepositToken.deposit`/`transfer`), and dust amounts of whitelisted underlyings — no privileged role, oracle manipulation, or trusted-remote compromise. Cost is bounded by the number of deposit/debt tokens in the pool (≤30). Modifiers do not prevent it: `whenNotPaused`/`nonReentrant` don't apply to the list-add check, `SynthContext` sender resolution doesn't change recipient accounting, and `onlyIfDepositTokenExists`/`doesDebtTokenExist` only verify the caller is a registered token.

### Recommendation
Do not add a list entry for unsolicited dust — e.g., only call `addToDepositTokensOfAccount` from `deposit`/`issue` (self-initiated mint paths) and skip list registration on `transfer`/`seize` recipients, or allow accounts to opt out / prune entries regardless of locked balances. Alternatively, track positions by a per-pool fixed bitmap of whitelisted tokens instead of a per-account enumerable list, eliminating the cap.

### Proof of Concept
Hardhat sketch (fork or local fixture):

```typescript
// setup: pool with N whitelisted DepositTokens registered
const victim = bob.address;

// Attacker holds dust of each underlying and dust-deposits on behalf of victim,
// or deposits to self and transfer()s 1 wei to the victim.
for (const dt of depositTokens) {
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying());
  await underlying.connect(attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, victim); // _mint -> addToDepositTokensOfAccount(victim)
}
// combine with debt-token entries via issue/mint paths until 30 reached
expect(await pool.getDepositTokensOfAccount(victim)).to.have.length(N);

// victim tries to deposit a collateral type not yet in their list
const newToken = depositTokensNotHeldByVictim[0];
const tx = newToken.connect(bob).deposit(parseEther('1'), victim);
await expect(tx).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// victim also cannot mint a new synthetic (addToDebtTokensOfAccount reverts)
const mintTx = debtToken.connect(pool.wallet).issue(1, victim);
await expect(mintTx).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Confidence caveat: exact feasibility of filling *debt-token* slots without victim cooperation was not fully traced (depends on whether `DebtToken.issue` allows issuing to arbitrary `to_`); the deposit-token half of the attack is confirmed by code at `contracts/DepositToken.sol:211-234,486-488,518-520` and `contracts/Pool.sol:143-148,204-220`. If debt tokens cannot be force-added, impact is limited to pools with enough deposit tokens to reach 30 combined entries, or requires saturating the remaining slots via repeated collateral types — verify against the deployed pool's `ReachedMaxDepositTokens` count in a fork PoC.