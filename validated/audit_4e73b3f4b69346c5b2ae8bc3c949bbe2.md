### Title
Griefing via dust `DepositToken` transfers/deposits fills `depositTokensOfAccount` to `MAX_TOKENS_PER_USER`, blocking the victim from adding collateral or issuing debt - ([File: contracts/DepositToken.sol](contracts/DepositToken.sol), [contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analogous to CVE-2021-2304 (a low-complexity attacker induced denial of service), an unprivileged attacker can cheaply and repeatedly DoS any Metronome account. `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once `debtTokensOfAccount + depositTokensOfAccount >= 30` (`MAX_TOKENS_PER_USER`). `DepositToken._transfer` and `_mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to >0, and `deposit()` accepts an arbitrary `onBehalfOf_`. The attacker therefore fills a victim's slot list with 1-wei dust positions in every enabled deposit token, causing all subsequent deposits, `DebtToken.issue`/`mint`, and incoming deposit-token transfers for *new* tokens to revert for that account.

### Finding Description
- `DepositToken._transfer` adds the recipient to the per-account set on first receipt (`_recipientBalanceBefore == 0 && amount_ > 0` → `pool.addToDepositTokensOfAccount(recipient_)`) at `contracts/DepositToken.sol:517-520`, and `_mint` does the same at `contracts/DepositToken.sol:485-488`.
- `DepositToken.deposit(uint256,address onBehalfOf_)` mints to an arbitrary beneficiary at `contracts/DepositToken.sol:211-237`, so the attacker doesn't even need the victim to accept a transfer — `deposit(1, victim)` suffices.
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when the combined count reaches 30 at `contracts/Pool.sol:143-148`; both `addToDepositTokensOfAccount` (`Pool.sol:216-220`) and `addToDebtTokensOfAccount` (`Pool.sol:204-208`) use it.
- Consequences once the victim's list is full:
  - `deposit()` of a collateral type the victim doesn't hold → `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens` revert.
  - `DebtToken.issue`/`mint` of a new synthetic → `addToDebtTokensOfAccount` → revert.
  - Any `transfer`/`transferFrom`/`seize` sending a new deposit token to the victim → revert (this also breaks `SmartFarmingManager` flows and liquidator payouts if the liquidator is the filled account).
- The attacker recovers nearly all cost: dust deposits are withdrawable, and the attack can be re-applied after the victim clears slots.

### Impact Explanation
The victim is blocked from depositing new collateral types and from issuing new debt. Critically, an unhealthy or soon-to-be-unhealthy victim cannot top up with a *new* collateral token to restore health — only tokens they already hold — enabling forced liquidation of positions that could otherwise be saved (temporary freezing of the deposit/borrow surface leading to loss of funds via liquidation). Slot freeing requires the victim to fully zero each dust balance, and the attacker can re-grief each emptied slot with another dust deposit in the same or next transaction, making the DoS persistent.

### Likelihood Explanation
Requires no privileged role: `deposit(amount_, onBehalfOf_)` is public and the dust amounts are minimal (1 wei of each underlying plus gas). Feasibility scales with the number of enabled deposit tokens (capped at 30 per pool by `addDepositToken`). The victim can partially mitigate by burning dust positions (transfer/withdraw full balance) but cannot prevent re-griefing, so the attack is repeatable at low cost.

### Recommendation
Make additions to `depositTokensOfAccount`/`debtTokensOfAccount` non-reverting for the *caller-initiated* path, or gate list membership behind a minimum (meaningful) balance threshold, or decouple the revert so that `deposit`/`issue` for a new token still succeeds while simply not tracking dust. E.g., in `DepositToken._mint`/`_transfer`, skip `addToDepositTokensOfAccount` for economically insignificant amounts, or enforce a minimum first-deposit amount per token.

### Proof of Concept
```ts
// Hardhat fork-style PoC (Pool.test.ts conventions)
// Assume: pool with >= N deposit tokens enabled; victim has an open position
// with some debt tokens already occupying slots.
const depositTokens = await pool.getDepositTokens(); // N tokens
const freeSlots = 30 - (await pool.getDepositTokensOfAccount(victim.address)).length
                 - (await pool.getDebtTokensOfAccount(victim.address)).length;

// Attacker fills all remaining slots with 1-wei dust deposits on behalf of victim
for (let i = 0; i < freeSlots; i++) {
  const dt = await ethers.getContractAt('DepositToken', depositTokens[i]);
  const underlying = await ethers.getContractAt('IERC20', await dt.underlying());
  await underlying.approve(dt.address, 1);
  await dt.deposit(1, victim.address); // mints 1 wei msdTOKEN to victim
}

expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(/* 30 - debtTokens */);

// Victim can no longer deposit a collateral type it doesn't already hold:
const newDt = await ethers.getContractAt('DepositToken', depositTokens[freeSlots]);
await expect(newDt.connect(victim).deposit(amount, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Nor issue a new debt token:
await expect(newDebtToken.connect(victim).issue(amount, victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// Nor receive transfers of a new deposit token:
await expect(dt.connect(attacker).transfer(victim.address, 1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```