### Title
Attacker can fill a victim's `depositTokensOfAccount`/`debtTokensOfAccount` list to `MAX_TOKENS_PER_USER` via dust `DepositToken` transfers, DoS-ing any new deposit or debt token for the victim - ([File: contracts/Pool.sol])

### Summary
This is the direct Metronome analog of the Velodrome `MAX_REWARD_TOKENS` fill attack. The pool keeps a per-account enumerable set of deposit and debt tokens, capped at `MAX_TOKENS_PER_USER = 30` (`Pool.sol:79`). Entries are added permissionlessly: `DepositToken._transfer` calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from 0 to >0 (`DepositToken.sol:517-520`), and `addToDepositTokensOfAccount` is guarded by `onlyIfAdditionWillNotReachMaxTokens`, which reverts with `UserReachedMaxTokens` once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`Pool.sol:143-148`, `Pool.sol:216-220`). An unprivileged attacker can `deposit` 1 wei of each whitelisted collateral and then `transfer` 1 wei of each `DepositToken` to a victim (`transfer`/`transferFrom` only run `_revertIfLocked` on the *sender*, `DepositToken.sol:348-376`), stuffing the victim's list until it hits 30.

### Finding Description
Once the victim's combined list length is 30:
- `DepositToken.deposit(amount_, victim)` for any collateral the victim doesn't already hold reverts inside `_mint → addToDepositTokensOfAccount` (`DepositToken.sol:486-488`).
- `DebtToken.issue` / `mint` for any synthetic the victim doesn't already owe reverts inside `_mint → addToDebtTokensOfAccount` (`DebtToken.sol:597-600`).
- `DepositToken.seize` also goes through `_transfer`, so liquidations where the seized collateral would be a new entry for the liquidator's own list can revert (`DepositToken.sol:343-345`).

Debt tokens are non-transferable (`TransferNotSupported`, `DebtToken.sol:507-519`), so the attacker can only push `depositTokensOfAccount` entries; feasibility requires the deployed pool to have enough whitelisted deposit tokens (combined with the victim's existing entries) to reach 30.

### Impact Explanation
DoS on adding new collateral or new debt tokens to a targeted account: the victim cannot onboard a new collateral type (including via `deposit(_, onBehalfOf_)`, `NativeTokenGateway.deposit`, or SmartFarmingManager flows that mint a new `DepositToken` to them) and cannot open a new synthetic debt position. No existing funds are frozen — withdrawals, transfers, and repayments of already-listed tokens still work, and the victim can shed an entry by zeroing its balance — so this is a liveness/griefing issue analogous in severity to the Velodrome M-04 (medium), not theft or insolvency. Note the reject criteria exclude pure gas/unbounded-loop DoS; this finding relies instead on a bounded-list capacity exhaustion of a storage invariant, the same class as the source report.

### Likelihood Explanation
Fully permissionless: only public `deposit`/`transfer` calls on already-registered `DepositToken`s are needed; cost is negligible (dust of each collateral). Feasibility depends on the number of whitelisted deposit tokens in the deployed pool — if the pool has fewer than 30 minus the victim's current entries, the cap cannot be reached and the attack fails. The victim can partially recover by emptying dusted positions, but cannot prevent re-filling.

### Recommendation
- Require a minimum transfer/deposit amount before an account is added to `depositTokensOfAccount` (e.g., only add on `deposit`, not on `transfer`), or make `addToDepositTokensOfAccount` on transfers opt-in.
- Alternatively bound list growth to minting paths only (deposit/issue), so unsolicited dust transfers cannot consume slots.

### Proof of Concept
Hardhat-style reproduction sketch:

```ts
// pool has >= 30 whitelisted DepositTokens; victim is a fresh EOA
const tokens: DepositToken[] = /* all registered deposit tokens */;

for (const dt of tokens.slice(0, MAX_TOKENS_PER_USER)) {
  const underlying = await dt.underlying();
  // attacker acquires dust collateral and deposits to own account
  await IERC20__factory.connect(underlying, attacker).approve(dt.address, 1);
  await dt.connect(attacker).deposit(1, attacker.address);
  // push 1 wei to victim -> adds dt to victim's depositTokensOfAccount
  await dt.connect(attacker).transfer(victim.address, 1);
}

expect(await pool.getDepositTokensOfAccount(victim.address)).to.have.lengthOf(30);

// victim can no longer receive any new deposit token
await expect(
  newDepositToken.connect(attacker).transfer(victim.address, 1)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// victim cannot deposit a collateral type they don't already hold
await expect(
  newDepositToken.connect(victim).deposit(1e18, victim.address)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");

// victim cannot issue a new synthetic debt token
await expect(
  newDebtToken.connect(victim).issue(parseEther("1"), victim.address)
).to.be.revertedWithCustomError(pool, "UserReachedMaxTokens");
```

Caveat: this requires the target pool to have enough distinct whitelisted deposit tokens for the attacker to reach the cap of 30 on a given victim; on pools with fewer registered collaterals the attack is bounded below the cap and does not trigger.