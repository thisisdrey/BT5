### Title
Unsolicited dust deposits permanently fill a victim's `depositTokensOfAccount` up to `MAX_TOKENS_PER_USER`, blocking collateral top-ups and new debt issuance (DoS) - (File: contracts/Pool.sol)

### Summary
`DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint deposit tokens to an arbitrary `onBehalfOf_` address. On the first non-zero balance, `_mint` calls `Pool.addToDepositTokensOfAccount(account_)`, which appends the token to the victim's per-account list. An unprivileged attacker can dust-deposit 1 wei of every listed collateral into a victim's account until `debtTokensOfAccount + depositTokensOfAccount == MAX_TOKENS_PER_USER` (30). From then on, any action that would add a new token to the victim's lists — depositing a new collateral type, receiving deposit tokens via transfer or `seize`, or issuing a new synthetic — reverts with `UserReachedMaxTokens`. Crucially, a victim whose position is unhealthy cannot evict the dust tokens, because `unlockedBalanceOf` returns 0 when `_issuableInUsd == 0`, so both `transfer` and `withdraw` revert via `_revertIfLocked`. The victim is locked out of the primary mechanism for restoring health (adding a different collateral) while the position is liquidatable.

### Finding Description
- `DepositToken.deposit` at `contracts/DepositToken.sol:211-237` pulls `underlying` from the caller and mints to `onBehalfOf_` with no opt-in from the beneficiary.
- `DepositToken._mint` at `contracts/DepositToken.sol:486-488` calls `pool.addToDepositTokensOfAccount(account_)` whenever the recipient's prior balance was 0.
- `Pool.addToDepositTokensOfAccount` / `addToDebtTokensOfAccount` at `contracts/Pool.sol:204-220` are gated by `onlyIfAdditionWillNotReachMaxTokens` (`contracts/Pool.sol:143-148`), which reverts `UserReachedMaxTokens` when the combined per-account lists reach `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79`).
- Removal only happens from inside `_burn`/`_transfer` when the balance reaches 0 (`contracts/DepositToken.sol:460-462`, `523-525`), i.e. via `withdraw` or `transfer`. Both are blocked by `_revertIfLocked` (`contracts/DepositToken.sol:180-182`, `348-353`, `406-411`) whenever `unlockedBalanceOf` is 0.
- `unlockedBalanceOf` (`contracts/DepositToken.sol:383-398`) returns `0` for the whole balance when the position has no issuable headroom (`_issuableInUsd == 0`), which is exactly the situation where the victim most needs to add collateral.
- Consequently the DoS is self-reinforcing: once the lists are full and the position is unhealthy, the victim cannot remove the attacker's dust entries, cannot deposit a new collateral type to heal the position, and cannot mint/receive anything that would add a list entry. Repaying debt still works (repay burns, doesn't add), but a user who cannot acquire the synthetic to repay, or whose only viable rescue path is depositing a new collateral asset, is forced into liquidation.
- The same mechanism blocks `DebtToken._mint` → `pool.addToDebtTokensOfAccount`, so issuing any new synthetic type also reverts for the victim, and a liquidator calling `Pool.liquidate` causes `seize` → `_transfer` which adds the token to the *liquidator's* list (self-inflicted, mitigable by using a fresh address, but still a revert path).

### Impact Explanation
Availability/DoS analog of the CVE: the victim's account is "hung" for all operations that append to the token lists. Concretely:
1. Victim with an open position near the liquidation threshold cannot deposit any collateral type they don't already hold — all such deposits revert `UserReachedMaxTokens` — while their existing collateral falls. Position gets liquidated; victim loses the liquidation incentive + protocol fee. This is temporary freezing of the collateral-deposit path translating into a forced loss.
2. Any user at the cap cannot issue new synthetic types or receive deposit tokens (transfers and liquidation `seize` to them revert).
Attacker cost is ~30 dust deposits of whitelisted collaterals (each as small as 1 wei gross, since `deposit` only requires `amount_ > 0` and `_mint` adds the entry for any `amount_ > 0` when balance was 0).

### Likelihood Explanation
Fully permissionless: no privileged role needed, works on the deployed configuration because `deposit`, `transfer`, `issue`, and `seize` are all public and the beneficiary is attacker-chosen. The only prerequisites are that the pool has multiple deposit tokens listed (true on deployed pools: multiple collaterals per pool) and that the combined count can reach 30 across deposit + debt token additions the attacker pushes onto the victim. The attack is repeatable (victim frees a slot, attacker re-dusts it cheaply) and is most damaging precisely when the victim is unhealthy, because locked balances make the dust non-removable until health is restored — a catch-22.

### Recommendation
- Do not add tokens to the per-account list for unsolicited receipts, or allow the account owner to remove entries: e.g., let `removeFromDepositTokensOfAccount`/`removeFromDebtTokensOfAccount` be callable by the account itself (which then forces a zero-balance check / small dust forfeiture), or add a `Pool.removeTokenFromMyList(depositToken_)` that sweeps the dust balance to the caller or fee collector.
- Alternatively, only append to `depositTokensOfAccount` on explicit user actions (deposit/mint by the account itself), not on `transfer`/`seize`/`onBehalfOf` mints; track "receiving" balances separately from position-relevant collateral.
- Note the DustToken removal already exists inside `_burn`/`_transfer`; the gap is that a locked victim cannot reach those paths, so an owner-callable removal is the minimal fix.

### Proof of Concept
Reproducible Hardhat sketch against existing fixtures (mirroring `test/Pool.test.ts` setup where `pool`, `msdMET`, `msEthDebtToken`, `feeProvider`, `masterOracle` exist):

```typescript
// given: pool with >= 30 listed deposit tokens D[0..29], victim alice with an open,
// near-limit position (healthy but _issuableInUsd == 0 or small)
// 1) Attacker deposits dust of every deposit token to alice
for (const d of depositTokens) {
  await underlying(d).approve(d.address, 1)   // attacker approves 1 wei
  await d.connect(attacker).deposit(1, alice.address) // mints dust, adds to alice's list
}
// alice's depositTokensOfAccount now fills remaining slots up to MAX_TOKENS_PER_USER
expect(await pool.getDepositTokensOfAccount(alice.address)).to.have.length( /* cap reached */)

// 2) Oracle drops collateral price => alice's position unhealthy (locked balances)
await masterOracle.updatePrice(met.address, lowerPrice)
const {_isHealthy} = await pool.debtPositionOf(alice.address)
expect(_isHealthy).false

// 3) Alice tries to free a slot: dust is locked, both paths revert
await expect(msdX.connect(alice).transfer(bob.address, 1))
  .revertedWithCustomError(msdX, 'NotEnoughFreeBalance')
await expect(msdX.connect(alice).withdraw(1, alice.address))
  .revertedWithCustomError(msdX, 'NotEnoughFreeBalance')

// 4) Alice tries to deposit a collateral type she does not yet hold => reverts
await newUnderlying.connect(alice).approve(newDepositToken.address, amount)
await expect(newDepositToken.connect(alice).deposit(amount, alice.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 5) Alice tries to mint a new synthetic type => reverts (debt token list add)
await expect(newDebtToken.connect(alice).issue(1, alice.address))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 6) Liquidator repays alice's debt and seizes collateral — forced liquidation succeeds
await pool.connect(liquidator).liquidate(msEth.address, alice.address, amountToRepay, msdMET.address)
```

Caveat: whether this exact griefing was already reported in prior Metronome audits cannot be fully verified from the repo index alone; the `MAX_TOKENS_PER_USER` cap and its tests (`test/Pool.test.ts:1386-1416`) show the limit is intentional, but no code prevents a third party from filling another account's list via `deposit(..., onBehalfOf_)` or dust `transfer`s, and no owner-triggered removal path exists, so the DoS stands on the deployed code.