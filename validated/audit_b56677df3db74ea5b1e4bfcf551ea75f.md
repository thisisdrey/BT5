### Title
Unprivileged dust-transfer griefing fills a victim's per-account token list (`MAX_TOKENS_PER_USER`), blocking their deposits and minting - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` tracks each account's deposit and debt tokens in `MappedEnumerableSet` lists capped at `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79`). Both `addToDepositTokensOfAccount` and `addToDebtTokensOfAccount` revert with `UserReachedMaxTokens` once the combined length reaches 30 (`contracts/Pool.sol:143-148`, `204-208`, `216-220`). `DepositToken._transfer`/`_mint` unconditionally call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance of that token was previously zero (`contracts/DepositToken.sol:485-488`, `517-520`). An unprivileged attacker who holds (or flash-borrows/deposits to mint) small amounts of many deposit tokens can transfer 1 wei of each to a victim, permanently occupying up to all 30 slots of the victim's account list.

### Finding Description
Analogous to CVE-2023-52725's "blocking a channel" liveness flaw, the per-account token list is a bounded shared resource that any EOA can fill on behalf of an arbitrary victim without their consent:

1. Attacker deposits collateral into each deposit token of the pool (or buys/mints dust `msdTOKEN`s) to obtain `>0` balance in up to 30 distinct `DepositToken`s.
2. Attacker calls `DepositToken.transfer(victim, 1)` for each. Each call executes `DepositToken._transfer`, sees `balanceOf[victim] == 0`, and calls `pool.addToDepositTokensOfAccount(victim)` (`contracts/DepositToken.sol:517-520`), which increments `depositTokensOfAccount.length(victim)`.
3. Once the victim's `debtTokensOfAccount + depositTokensOfAccount` reaches 30, the `onlyIfAdditionWillNotReachMaxTokens` modifier (`contracts/Pool.sol:143-148`) makes every subsequent first-time token acquisition revert:
   - Victim cannot deposit a new collateral type (`DepositToken._mint` → `pool.addToDepositTokensOfAccount` reverts).
   - Victim cannot mint a new synthetic asset (`DebtToken.issue/mint` → `pool.addToDebtTokensOfAccount` reverts).
   - Any `msdTOKEN` transfer from a token the victim doesn't already hold reverts in `_transfer`.

The attacker needs no privilege; `transfer` on `DepositToken` is a public ERC-20 entry point and only the attacker's own `_revertIfLocked`/`unlockedBalanceOf` checks apply to the sender side. No pause flag, reentrancy guard, or SynthContext check prevents a recipient-side list insertion.

### Impact Explanation
Liveness / temporary freezing of funds: the victim is blocked from opening positions with any new collateral or synthetic token, and cannot receive any `msdTOKEN` they do not already hold. Recovery requires the victim to transfer each dust token back out so `balanceOf` returns to 0 and `removeFromDepositTokensOfAccount` fires (`contracts/DepositToken.sol:522-525`) — the victim pays gas for up to 30 forced cleanup transactions and, if any dust lands in a token whose transfer would leave a locked-balance shortfall, may be unable to remove it while their position is collateralized. This is a repeatable, cheap, unprivileged griefing vector against any account.

### Likelihood Explanation
High reachability: any EOA can call `DepositToken.transfer`. Cost is ~30 token transfers plus the dust capital (which is recovered on withdrawal). The main mitigations are that existing deposit/debt tokens keep working, and the victim can self-clean the list at gas cost — so the impact is temporary freeze rather than permanent loss.

### Recommendation
- Do not add tokens to an account's list inside `DepositToken._transfer` for the recipient; only track tokens acquired via `deposit`/`mint`, or make list insertion lazy/opt-in.
- Alternatively, only count tokens whose balance exceeds a meaningful threshold, or raise/remove the shared 30-slot cap split between deposit and debt tokens.
- At minimum, allow the victim (or anyone) to call a `removeFromDepositTokensOfAccount`-style purge for zero-balance entries.

### Proof of Concept
Hardhat fork sketch:

```ts
// pool, depositTokens[0..29] are live DepositToken contracts
for (let i = 0; i < 30; i++) {
  // attacker obtains dust of depositTokens[i] via deposit() or buy
  await depositTokens[i].connect(attacker).transfer(victim.address, 1);
}
expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.length(30);

// victim tries to deposit a new collateral type -> reverts
await expect(
  newDepositToken.connect(victim).deposit(1)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// victim tries to mint a new synthetic -> reverts
await expect(
  newDebtToken.connect(victim).issue(1)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// cleanup: victim must transfer each dust token out
await depositTokens[0].connect(victim).transfer(attacker.address, 1);
```