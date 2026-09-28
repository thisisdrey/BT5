### Title
Attacker permanently blocks a victim from onboarding collateral/debt positions by dust-filling their `MAX_TOKENS_PER_USER` account list — ([File: contracts/DepositToken.sol](contracts/DepositToken.sol), [File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
Analog of CVE-2020-10101 (unhandled malformed input → service crash / DoS): in Metronome, an unprivileged attacker can weaponize an unchecked input condition — receiving deposit tokens from a zero-balance account — to make every subsequent position-changing call for a victim revert. `Pool` maintains a per-account `MappedEnumerableSet` capped at `MAX_TOKENS_PER_USER = 30` (combined debt + deposit tokens). `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once the cap is hit, and there is no way for the victim to remove entries they do not control.

### Finding Description
`DepositToken._transfer` automatically registers the deposit token on the recipient's account list whenever the recipient's balance transitions from zero to non-zero (`contracts/DepositToken.sol:518-520`):

```solidity
if (_recipientBalanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(recipient_);
}
```

`transfer`/`transferFrom` are public and unrestricted to `recipient_`; any EOA can send 1 wei of each `msd*` token to any victim. The registration path enforces the cap and reverts (`contracts/Pool.sol:143-148`):

```solidity
modifier onlyIfAdditionWillNotReachMaxTokens(address account_) {
    if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
        revert UserReachedMaxTokens();
    }
    _;
}
```

The same revert path is hit by `DepositToken._mint` (`contracts/DepositToken.sol:486-488`) — so `deposit(amount, to)` to a saturated account reverts — and by `DebtToken` issuance for new debt tokens via `addToDebtTokensOfAccount` (`contracts/Pool.sol:204-208`). Because `_transfer` performs the registration inside the same transaction as the balance update, the revert is atomic: the attacker incurs only gas plus permanently dusted tokens, and the victim cannot delete the attacker's entries (removal only happens when the victim's own balance of that token returns to zero, i.e., only the victim can spend their own dust out — but only for tokens they actually hold; entries for tokens the attacker dusted them with are stuck until the victim manually transfers each dust amount out, one tx per token, and the attacker can re-dust at will since re-adding is always allowed once balance returns to 0 and the list has room).

### Impact Explanation
Once a victim's combined list reaches 30 entries:
- They cannot receive any *new* deposit token type — `transfer`, `deposit(..., to_)`, liquidation `seize` proceeds directed at them, and `DebtToken.issue` for a new synthetic all revert.
- A victim with an open position approaching the liquidation threshold cannot deposit a collateral type they do not already hold to restore health, and cannot mint a new synthetic to repay; if their existing single collateral is insufficient, forced liquidation and loss of the collateral discount follows.
- Liveness invariant (ability to open/extend positions) is broken for that account indefinitely; the attack is repeatable at negligible cost (1 wei per token) and cannot be prevented by the victim, matching the CVE's "crash the service via an unchecked message" pattern.

### Likelihood Explanation
Requires only EOA calls to public `transfer`/`deposit` functions and ownership of dust amounts of each enabled `msd*` token (obtainable by depositing minimal underlying). No privileged role, oracle manipulation, or reentrancy needed. Effectiveness depends on the number of deposit tokens enabled per pool; pools approaching 30 offerings make saturation cheap and complete, and even partial filling (victim already using several slots) achieves the same cap.

### Recommendation
- Do not auto-register tokens on `transfer`/`deposit`; only register on explicit user action (e.g., `deposit` to self or an opt-in `enableCollateral` call), so unsolicited dust cannot occupy slots.
- Alternatively, allow `removeFromDepositTokensOfAccount` to be called by the account itself (not just the token contract) when its balance is zero, letting victims evict attacker-inserted entries.
- Raise or bound-check `MAX_TOKENS_PER_USER` against the number of enabled tokens to guarantee headroom.

### Proof of Concept
Hardhat sketch (fork the pool's deployed config where N deposit tokens are enabled):

```ts
// attacker holds 1 wei of each msdToken
for (const msd of depositTokens) {
  await msd.connect(attacker).transfer(victim.address, 1); // fills depositTokensOfAccount[victim]
}
// victim already has debt tokens or some deposits; combined length hits 30
expect(await pool.getDepositTokensOfAccount(victim.address)).length
  .to.eq(MAX - debtTokensOfVictim);

// any new-token interaction now reverts
await expect(
  msdNew.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

await expect(
  msdNew.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// victim cannot mint a new synthetic debt token either
await expect(
  pool.connect(victim).issue(synthNew, amount)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```