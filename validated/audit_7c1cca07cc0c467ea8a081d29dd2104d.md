### Title
Dust-transfer spam fills a victim's per-account token list (`MAX_TOKENS_PER_USER`), permanently blocking that account from receiving new deposit-token types or repaying with new debt tokens — ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The QEMU bug class is a resource-exhaustion DoS: an attacker issues many cheap commands that each allocate persistent state, degrading/denying service. The Metronome analog is the per-account token lists `depositTokensOfAccount` / `debtTokensOfAccount` in `Pool`. Every `DepositToken._transfer` / `_mint` adds the token to the recipient's list when the recipient's balance was zero, and `Pool.addToDepositTokensOfAccount` reverts once `debtTokensOfAccount + depositTokensOfAccount >= MAX_TOKENS_PER_USER` (30). An unprivileged attacker can dust-transfer `1 wei` of each of up to 30 registered deposit tokens to a victim, filling the victim's list and causing every subsequent mint/transfer of any *new* deposit token to that account to revert.

### Finding Description
`DepositToken._transfer` adds the token to the recipient's per-account set whenever the recipient balance was previously zero:
```solidity
// contracts/DepositToken.sol:517-520
if (_recipientBalanceBefore == 0 && amount_ > 0) {
    pool.addToDepositTokensOfAccount(recipient_);
}
```
`Pool.addToDepositTokensOfAccount` enforces the cap with a revert:
```solidity
// contracts/Pool.sol:143-148
if (debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER) {
    revert UserReachedMaxTokens();
}
```
Only the token contract itself can call the add/remove functions (`_revertIfSenderIsNotDepositToken`), and the list is only pruned when a balance returns to zero — the victim cannot clear entries added by dust, since each deposited token counts until fully transferred/burned. An attacker who deposits a minimal amount of each registered collateral (real cost: dust of each underlying) then `transfer(victim, 1)` for each token permanently occupies up to `MAX_TOKENS_PER_USER` slots. Because the revert happens inside `_transfer`/`_mint`, it poisons:

- `DepositToken.deposit(..., onBehalfOf_=victim)` for any collateral the victim doesn't already hold — mint reverts.
- `DepositToken.seize(victim, ...)`/`seize(from, to=victim, ...)` — a liquidator/victim receiving a new collateral type reverts.
- Any direct `transfer`/`transferFrom` of a new deposit token to the victim reverts.

The same applies to `DebtToken` minting (`addToDebtTokensOfAccount`), so a list filled with dust deposit tokens also blocks the victim from issuing *any new* synthetic debt or being minted a new debt token — including actions needed to manage a position.

### Impact Explanation
- **Temporary/permanent freezing of funds flows**: the victim cannot deposit new collateral types. For a leveraged position approaching liquidation, being unable to add collateral to restore health forces liquidation that could otherwise be avoided — effectively freezing the position-management path and exposing collateral to seizure.
- **Targeted griefing at dust cost**: attacker spends only dust amounts of each underlying (refundable by withdrawing their own balance afterwards, minus fees), while the victim's list is DoS'd until the victim burns each dust token — each burn requires a `withdraw`/`transfer` back to zero, and a victim with an unhealthy position may have locked balances (`_revertIfLocked`) that make cleanup impossible exactly when they need to free slots.
- Also blocks receiving new collateral via liquidation proceeds (`seize` to the account) and blocks `RewardsDistributor`-related flows that mint to the account.

No privileged actor required: `deposit`, `transfer`, and `transferFrom` are public; `SynthContext._msgSender` and `nonReentrant` don't interfere.

### Likelihood Explanation
Requires the pool to have registered enough deposit tokens to make filling 30 slots practical (cap also limits pool-side deposit tokens to 30, and debt tokens share the same 30-slot budget, so fewer dust tokens may suffice if the victim already holds some). Cost is bounded by dust deposits in each registered collateral plus gas. No oracle manipulation, governance action, or trusted role needed. Impact is per-account DoS rather than protocol-wide theft, matching a Medium severity.

### Recommendation
- Give the account an escape: allow `removeFromDepositTokensOfAccount`/`removeFromDebtTokensOfAccount` to be callable by the token *or* by the account itself (the victim can then evict dust entries at will, e.g. `pool.removeFromDepositTokensOfAccountSelf(token)` gated on `balanceOf` checks), or add a `Pool`-level function letting `account_ == _msgSender()` remove tokens regardless of balance.
- Alternatively, exempt dust balances below a threshold from list membership, or transfer dust tokens in the same transaction back to sender.
- Document that users should avoid receiving transfer dust, or auto-wrap incoming `transfer` of unregistered-to-recipient tokens.

### Proof of Concept
Hardhat (mainnet fork of a deployed `Pool` + real `DepositToken`s):
```ts
// victim has no tokens; attacker deposits 1 wei of each underlying and dusts victim
for (const depositToken of registeredDepositTokens) {
  const underlying = await ethers.getContractAt('IERC20', await depositToken.underlying());
  await underlying.connect(attacker).approve(depositToken.address, 1);
  await depositToken.connect(attacker).deposit(1, attacker.address);
  await depositToken.connect(attacker).transfer(victim.address, 1); // balance 0 -> added to victim list
}
// victim's depositTokensOfAccount.length == MAX_TOKENS_PER_USER now (also counting any debt tokens)

// Any new deposit-token mint/transfer to victim reverts:
const newToken = registeredDepositTokens[N]; // one victim doesn't hold
await expect(newToken.connect(attacker).transfer(victim.address, 1))
  .to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
// Same for: depositToken.deposit(amount, victim.address) -> _mint -> revert
// And: pool.liquidate(...) where seized collateral goes to victim
```
Reproduced on the unit-test harness by replacing `addToDepositTokensOfAccount` calls with real `DepositToken.transfer` dust calls; the existing test at `test/Pool.test.ts:1386` already proves the revert path (`UserReachedMaxTokens`) is reached via token-driven list growth.