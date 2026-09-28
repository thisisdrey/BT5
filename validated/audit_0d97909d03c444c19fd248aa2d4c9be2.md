### Title
Dust-deposit griefing fills victim's `depositTokensOfAccount` list and blocks new deposits/mints via `MAX_TOKENS_PER_USER` - (File: contracts/Pool.sol)

### Summary
An unprivileged attacker can force arbitrary addresses into a victim's per-account token lists by calling `DepositToken.deposit(amount_, onBehalfOf_ = victim)` with dust amounts (or `transfer`ing dust `msdTOKEN`s to the victim). Once the combined `depositTokensOfAccount + debtTokensOfAccount` list reaches `MAX_TOKENS_PER_USER` (30), every `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` call for that account reverts with `UserReachedMaxTokens`, which DoSes the victim's ability to deposit new collateral types and issue new synthetic debt types.

### Finding Description
`DepositToken.deposit()` accepts an arbitrary `onBehalfOf_` beneficiary and mints the deposit token to that account (contracts/DepositToken.sol:211-237). Internally, `_mint` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's balance transitions from zero (contracts/DepositToken.sol:486-488). The same happens on `transfer`/`transferFrom` via `_transfer` (contracts/DepositToken.sol:518-520).

On the `Pool` side, `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens`, which enforces `MAX_TOKENS_PER_USER = 30` (contracts/Pool.sol:79, 204-220). The list is keyed on the *recipient* account, not the payer — so the attacker controls the addition while the victim owns the entry.

Attack steps:
1. Attacker acquires small amounts of each underlying supported by the pool.
2. For each `DepositToken` the victim does not yet hold, attacker calls `deposit(1 wei, victim)` (attacker pays the underlying; victim is minted dust msdTOKEN and gets the token appended to `depositTokensOfAccount`).
3. Repeat until the victim's combined list length is 30. Attacker can also directly `transfer` dust msdTOKENs instead of depositing.

From then on, any transaction that would add a *new* token to the victim's lists reverts: `deposit` of a not-yet-held collateral reverts inside `_mint` → `addToDepositTokensOfAccount`; `DebtToken.issue` of a not-yet-held synthetic reverts inside `addToDebtTokensOfAccount`; even plain ERC20 `transfer` of a new deposit token *to* the victim reverts.

### Impact Explanation
This is a temporary freezing / liveness violation of the victim's position, matching the availability-only DoS class of CVE-2021-2028. The victim cannot:
- deposit additional collateral of any new type — critically, this blocks the standard self-rescue path for a position approaching liquidation when the victim doesn't already hold the collateral they'd want to top up with;
- mint any new synthetic debt type;
- receive any new deposit token via transfer or liquidation `seize` proceeds.

Recovery requires the victim to spend gas to transfer the attacker-donated dust out of their wallet (each `transfer` removing one entry), so the DoS persists until manual cleanup. No privileged role, oracle manipulation, or governance action is needed — the attack uses only public `deposit`/`transfer` entry points and the attacker's own tokens.

### Likelihood Explanation
The attack requires only dust amounts of pool-supported underlyings (one wei-level deposit or transfer per slot, up to 30) — cheap on L2s/Base/Optimism where the pool is deployed. Any EOA can execute it permissionlessly against any target. Mitigations: the victim's existing positions remain withdrawable and the victim can clear slots at gas cost, so the impact is temporary freezing of protocol functionality for the victim rather than permanent loss — consistent with medium severity.

### Recommendation
- Restrict `DepositToken.deposit`'s `onBehalfOf_` minting so unsolicited first-time deposits cannot be pushed onto a victim's list (e.g., only allow `addToDepositTokensOfAccount` when `account_ == _msgSender()`, or let users "claim" a list slot).
- Alternatively, do not revert on reaching the cap during `transfer`/`seize`/`deposit` for the *recipient*; instead allow balances outside the enumerable list, or decouple the health-check iteration from transfer bookkeeping.
- Consider refunding/gas-budgeting cleanup or removing the shared 30-slot cap in favor of iterating a separate, opt-in collateral set.

### Proof of Concept
Hardhat sketch (fill victim's list, then show deposit revert):

```ts
// given: pool with N >= 30 deposit tokens registered (depositTokens[i])
const victim = bob.address

// attacker fills victim's account list with dust
for (let i = 0; i < 30; ++i) {
  const dt = depositTokens[i] // DepositToken not yet held by victim
  await underlying[i].connect(attacker).approve(dt.address, 1)
  await dt.connect(attacker).deposit(1, victim)   // onBehalfOf_ = victim
}
expect(await pool.getDepositTokensOfAccount(victim)).to.have.length(30)

// victim tries to deposit a collateral type not already in the list
await newUnderlying.connect(bob).approve(newDepositToken.address, amount)
await expect(
  newDepositToken.connect(bob).deposit(amount, victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// victim also cannot mint a synthetic whose debt token isn't already listed
await expect(
  newDebtToken.connect(bob).issue(1, victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Note: I verified the core mechanics (`deposit` beneficiary, `_mint`/`_transfer` list insertion, `MAX_TOKENS_PER_USER` revert path and the existing test at test/Pool.test.ts:1386-1416 proving the revert), but the exact body of `onlyIfAdditionWillNotReachMaxTokens` (whether the 30 cap counts deposit+debt lists jointly) was not fully read; the test file confirms the cap is enforced across both lists.