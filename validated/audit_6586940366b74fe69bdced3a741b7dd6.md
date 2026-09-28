### Title
Unconsented dust deposits fill a victim's per-account token list to `MAX_TOKENS_PER_USER`, blocking deposits, transfers and new debt issuance - (File: contracts/Pool.sol)

### Summary
`Pool` tracks each account's deposit/debt tokens in `MappedEnumerableSet` lists capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79`). Any addition past the cap reverts with `UserReachedMaxTokens` (`contracts/Pool.sol:143-148`). Because `DepositToken.deposit(amount_, onBehalfOf_)` mints to an arbitrary beneficiary with no opt-in, and `_transfer`/`_mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` for first-time holders, an attacker can forcibly populate a victim's list with dust positions.

### Finding Description
- `DepositToken.deposit` accepts any `onBehalfOf_` and calls `_mint`, which adds the token to the beneficiary's account list on first receipt (`contracts/DepositToken.sol:211-236`, `contracts/DepositToken.sol:485-488`).
- `DepositToken._transfer` does the same for unsolicited incoming transfers (`contracts/DepositToken.sol:517-520`).
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` are gated by `onlyIfAdditionWillNotReachMaxTokens` and revert once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148`, `contracts/Pool.sol:204-220`).
- `DebtToken.issue`/`_transfer` follows the same pattern for the debt list, so debt tokens also count toward the cap.

Attack: the attacker deposits 1 wei of every deposit token in the pool (and issues/transfers dust debt where supported) with `onBehalfOf_ = victim`, filling the victim's combined list to 30 entries.

### Impact Explanation
Once the victim's list is full:
- Any `deposit` into a collateral the victim doesn't already hold reverts (`UserReachedMaxTokens`), including attempts to add new collateral to rescue an unhealthy position.
- Any incoming msdTOKEN transfer of a token the victim doesn't already hold reverts, so the victim cannot receive collateral tokens.
- The victim cannot be onboarded to new debt tokens.

This mirrors CVE-2019-19313's class: attacker-supplied state makes core operations fail for a specific account. The freeze is temporary — the victim can `withdraw`/transfer out the dust (unlocked balances have no lock) to shrink the list — but the attacker can front-run and re-fill, forcing a gas-costly cat-and-mouse and reliably griefing targeted transactions (e.g., a victim racing to add collateral before liquidation).

### Likelihood Explanation
Feasibility depends on the pool having enough distinct deposit + debt tokens for the attacker to reach 30 entries; on pools with few collaterals the cap cannot be reached by an external attacker alone. Where the token count is sufficient, the attack needs only standard deposits/dust transfers from an unprivileged EOA — no privileged role, oracle fault, or trusted-remote failure. No pause flag, reentrancy guard, or SynthContext check mitigates it.

### Recommendation
Make list membership opt-in or attacker-resilient:
- Reject `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` for entries added by transfers/deposits not initiated by the beneficiary, or
- Only track tokens once the position exceeds a meaningful minimum (e.g., USD floor), or
- Let the account itself prune entries (`removeFrom...` callable by the account when balance is dust/zero) and/or allow deposits to bypass the cap for tokens they already hold (already the case, but ensure new-token deposits can't be permanently griefed by letting victims remove arbitrary zero/near-zero entries cheaply).

### Proof of Concept
Hardhat sketch (fork or fixture with a deployed `Pool` having ≥30 deposit+debt tokens):

```ts
// attacker fills victim's list
for (const dt of depositTokens) {
  await underlying.connect(attacker).approve(dt.address, 1)
  await dt.connect(attacker).deposit(1, victim.address) // onBehalfOf = victim
}
for (const debtToken of debtTokens) {
  // issue/transfer dust debt to victim where supported
}
// victim can no longer onboard a new collateral
await expect(
  newDepositToken.connect(victim).deposit(parseEther('10'), victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
// and cannot receive msdTOKEN transfers of new tokens
await expect(
  someDepositToken.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
```

Note: this PoC requires a pool configuration where the attacker can reach 30 distinct registered deposit/debt tokens; if deployed pools expose fewer tokens to unprivileged users, the cap is unreachable and the finding does not hold on that configuration.