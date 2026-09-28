### Title
Griefing via dust deposit-token transfers fills a victim's per-account token list to `MAX_TOKENS_PER_USER`, permanently blocking collateral top-ups and forcing liquidation - ([File: contracts/Pool.sol])

### Summary
The python-socketio advisory is a resource-accumulation DoS: the server holds attacker-supplied partial messages in memory indefinitely because it never bounds or cleans up pending state. The Metronome analog is per-account token-list exhaustion. The `Pool` tracks each account's deposit and debt tokens in `MappedEnumerableSet` lists capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79,143-148`). Any account can grow another account's `depositTokensOfAccount` list by transferring it dust `msdTOKEN`s, since `DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` when the recipient balance goes from 0 to >0 (`contracts/DepositToken.sol:517-520`). Once the combined list hits 30, every operation that would add a new token to the victim's position reverts — deposits of new collateral types, receipt of seized tokens, and issuance of new debt (`Pool.addToDebtTokensOfAccount` enforces the same cap, `contracts/Pool.sol:204-208`).

### Finding Description
- `Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` when `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148, 216-220`).
- `DepositToken.deposit(amount_, onBehalfOf_)` mints to an arbitrary `onBehalfOf_`, and plain `transfer`/`transferFrom` also add the token to the recipient's list (`contracts/DepositToken.sol:211-237, 486-488, 517-520`). The attacker therefore only needs to deposit dust of each registered underlying and `transfer` 1 wei of each `msdTOKEN` to the victim — ~30 cheap calls, fully permissionless.
- The only way for the victim to shrink the list is to drive a balance to zero via `transfer`/`withdraw`, which is gated by `_revertIfLocked` → `unlockedBalanceOf` (`contracts/DepositToken.sol:350-353, 383-398`). When the position's `_issuableInUsd == 0` (i.e. at or beyond its collateral limit — exactly when a top-up is needed), `unlockedBalanceOf` returns 0 for every token, so the victim **cannot** clear the attacker-inserted slots. The dust slots are then permanent for that account.
- Result: the victim cannot `deposit` a different collateral to restore health, cannot `issue` new synth debt, and cannot receive `seize`d tokens — while `liquidate` still works against them.

### Impact Explanation
An attacker fills a target's token list while its position is healthy. When the collateral price drops and the position approaches or crosses the issuable limit, the victim's normal defense — depositing a *different* collateral type — reverts with `UserReachedMaxTokens`, and the victim cannot evict the dust entries because all transfers are locked. The position is then liquidated and the victim loses collateral plus liquidation fees. This is a temporary/permanent freezing-of-funds griefing that converts directly into forced liquidation losses — mirroring the advisory's "unbounded pending-state accumulation → denial of service" class.

### Likelihood Explanation
- Fully unprivileged: the attacker uses only public `deposit`/`transfer` entry points with their own funds; no governor/keeper/oracle assumptions.
- Cost is bounded: attacker must acquire dust of up to ~30 registered deposit tokens, each requiring a dust underlying deposit plus a 1-wei transfer. Existing deposit tokens held by the victim count toward the cap, reducing required fills.
- Limitations: (a) if the victim stays healthy, they can transfer the dust away to reclaim slots; (b) the victim can still deposit *existing* collateral types and repay debt, so impact materializes only when a new collateral type or new debt token is needed. This makes it a targeted griefing rather than a global DoS.

### Recommendation
- Only add a token to `depositTokensOfAccount` when the recipient's balance crosses zero **and** the mint came from the recipient's own action, or allow removal of zero-valued entries by anyone (e.g. a `sweepDustFromAccount(account, token)` that any caller can invoke when `balanceOf(account) == 0` — though here the dust *is* the balance, so alternatively permit the account to always clear slots regardless of lock status).
- Simplest fix: exempt the dust-removal path — let an account transfer out (or burn) tokens even when fully locked, since removing the token can only reduce collateral voluntarily; or raise/eliminate the per-account cap by computing `depositOf`/`debtOf` over the pool-level token lists instead of per-account lists.

### Proof of Concept
```solidity
// Hardhat fork test sketch
// assume pool has >= N registered depositTokens, victim holds depositToken[0] + debt
for (uint i = 1; i < 30 - debtTokenCount - victimDepositCount; ++i) {
    underlying[i].approve(depositToken[i], DUST);
    depositToken[i].deposit(DUST, attacker);      // mint dust to attacker
    depositToken[i].transfer(victim, DUST);       // fills victim's depositTokensOfAccount
}
// victim's list length == 30

// oracle price of victim's collateral drops so issuableInUsd == 0
// victim attempts cleanup: reverts — unlockedBalanceOf(victim) == 0
await expect(depositToken[i].connect(victim).transfer(attacker, DUST))
    .to.be.reverted; // TransferAmountExceedsUnlockedBalance

// victim attempts to deposit a new collateral type: reverts
await expect(depositToken[j].connect(victim).deposit(amount, victim))
    .to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// liquidator seizes the position
await pool.connect(liquidator).liquidate(syntheticToken, amount, victim, depositToken[0]);
```
Key code paths: `Pool.sol:143-148` (cap check), `Pool.sol:216-220` (add on mint/transfer), `DepositToken.sol:383-398` (locked balance blocks dust cleanup), `DepositToken.sol:517-520` (recipient-side list growth on any transfer).