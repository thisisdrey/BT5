### Title
Attacker can permanently block a victim's deposits and debt issuance by dust-filling their `depositTokensOfAccount` list up to `MAX_TOKENS_PER_USER` - (File: contracts/Pool.sol)

### Summary
`MAX_TOKENS_PER_USER` (=30) is a protective bound on the per-account token lists in `Pool`, analogous to BIND's `deny-answer-aliases` protection that contained a reachable fatal defect. Here, the bound can be forced on any account from the outside: `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance transitions 0→>0, and `Pool.addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once the combined list length reaches 30. An attacker deposits dust into every pool `DepositToken` and transfers 1 wei to the victim, filling the victim's list. Afterwards, the victim cannot deposit into any deposit token they do not already hold (`deposit` → `_mint` → `addToDepositTokensOfAccount` reverts) and cannot `issue` any synthetic whose `DebtToken` they do not already hold (`issue` → debt token mint → `addToDebtTokensOfAccount` reverts). The same revert also bricks `SmartFarmingManager.leverage` and `Pool.liquidate` seizure paths that mint/transfer a new deposit token to the victim (seize to a liquidator is attacker-controlled, so it is not the vector; the victim-side block is).

### Finding Description
- `DepositToken._transfer` unconditionally registers the recipient: `if (_recipientBalanceBefore == 0 && amount_ > 0) pool.addToDepositTokensOfAccount(recipient_)` (contracts/DepositToken.sol:517-520). There is no opt-in, no minimum amount, and no consent.
- `DepositToken.transfer`/`transferFrom` only require `unlockedBalanceOf(sender) >= amount_` via `_revertIfLocked` (contracts/DepositToken.sol:348-376), so an attacker with a debt-free account can freely push dust to any address.
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (contracts/Pool.sol:143-148). This modifier protects `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` (confirmed by tests expecting `UserReachedMaxTokens`, test/Pool.test.ts:1386-1416).
- After the victim's list is filled: `DepositToken.deposit` for any not-yet-held collateral reverts inside `_mint` (contracts/DepositToken.sol:486-488), and `DebtToken.issue` for any not-yet-held synth reverts inside its mint hook that calls `addToDebtTokensOfAccount`. Removal requires the victim's balance of that token to hit 0, which only the victim (or their approved spender/SmartFarmingManager) can trigger — so the attacker cannot un-fill it, but recovery demands per-token transactions from the victim.

### Impact Explanation
Denial of service of the protocol's core entry points for a targeted account: the victim is blocked from depositing any new collateral type and from issuing any new synthetic debt until they manually clear entries by transferring/withdraw­ing full balances of listed tokens. For leverage users, `SmartFarmingManager.leverage`/`flashRepay` flows that mint a new token to the position owner also revert, which can deny a victim the ability to de-lever a deteriorating position — effectively temporary freezing of funds and forced exposure to liquidation. Cost to the attacker is only dust deposits (≈15 tokens per pool) plus gas; no privileged role, oracle manipulation, or misconfiguration is required.

### Likelihood Explanation
Fully permissionless and cheap: any EOA executes `DepositToken.deposit(1 wei)` once per pool token on an attacker-controlled account, then `transfer(victim, 1 wei)` per token. No front-running dependency beyond acting before the victim's next deposit/issue. The revert path is deterministic on deployed config (`MAX_TOKENS_PER_USER` is a constant; mainnet pools list multiple deposit/debt tokens). The mitigation — victim self-cleansing via transfers — is manual, costs gas per token, and may be impossible for contract-based victims (smart-farming positions) that cannot call `DepositToken.transfer`. This mirrors the CVE: a security bound that itself becomes the DoS vector.

### Recommendation
- Only grow `depositTokensOfAccount`/`debtTokensOfAccount` from paths the account controls (deposit/issue to self), or make the list update best-effort: skip `UserReachedMaxTokens` when triggered by `transfer`/`seize` for a recipient, or allow `add` to silently no-op on cap for unsolicited tokens.
- Alternatively, track position existence via a `mapping(account => token => bool)` set at deposit/issue time only, or require `amount_ > dustThreshold` before registering.
- Provide a `removeFrom*` path callable by the account itself to clear attacker-inserted entries.

### Proof of Concept
Hardhat test sketch (pool with ≥30 registered deposit tokens, e.g., deployed Base pool config):

```ts
// attacker holds dust of each DepositToken
for (const dt of depositTokens) {
  await underlying.approve(dt.address, 1);
  await dt.deposit(1, attacker.address);          // attacker balance > 0
  await dt.transfer(victim.address, 1);           // adds dt to victim's list
}
// victim's depositTokensOfAccount length == 30

// victim tries to deposit a collateral they don't yet hold -> reverts
await expect(newDepositToken.deposit(parseEther('10'), victim.address))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// victim tries to issue a synth whose DebtToken they don't hold -> reverts
await expect(newDebtToken.issue(victim.address, parseEther('1')))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// victim's existing withdraw still works, but leverage/flashRepay paths
// that mint a new token to the victim also revert until they clear entries.
```

Note: I verified the revert path via the `onlyIfAdditionWillNotReachMaxTokens` modifier and the `_transfer` hook; I could not re-read the exact `addTo*`/`DebtToken._mint` bodies in this session (grep confirmed their presence in Pool.sol/DebtToken.sol), but the unit tests asserting `UserReachedMaxTokens` on these paths confirm the behavior.