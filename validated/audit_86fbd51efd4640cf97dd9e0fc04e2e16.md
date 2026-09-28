### Title
Attacker can permanently fill a victim's per-account token list with dust msdTOKEN transfers, blocking all new collateral deposits and debt — ([File: contracts/DepositToken.sol](contracts/DepositToken.sol))

### Summary
`DepositToken._transfer` (and `_mint`) registers the token in `Pool.depositTokensOfAccount[recipient]` whenever the recipient's balance moves from zero to non-zero. `Pool.addToDepositTokensOfAccount` enforces `MAX_TOKENS_PER_USER = 30` via `onlyIfAdditionWillNotReachMaxTokens`, and there is no opt-in: an attacker can push dust `msdTOKEN` to any victim for each listed collateral until the victim's combined deposit+debt token list hits 30. Once full, any subsequent `deposit()` or `issue()`/`mint()` that would introduce a *new* token reverts with `UserReachedMaxTokens`, echoing CVE-2017-3144's pool-descriptor exhaustion: slots are allocated to the recipient without consent and only freed when the recipient's balance returns to exactly zero — which the victim often cannot do because withdrawals/transfers are gated by `_revertIfLocked`/`unlockedBalanceOf` once they carry debt.

### Finding Description
- `DepositToken._transfer` adds the token to the recipient's per-account set unconditionally on a first-time receipt (`contracts/DepositToken.sol:517-520`); `_mint` does the same (`contracts/DepositToken.sol:485-488`).
- `Pool.addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` revert once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148, 204-220`).
- Entries are only removed when `balanceOf[account] == 0` (`DepositToken._burn`/`_transfer`, `DebtToken._burn` `contracts/DebtToken.sol:539-542`).
- The only sender-side guard is `_revertIfLocked(sender, amount)` — the recipient's lock state is never checked, so dust lands even on fully-collateralized-locked victims.
- Attack path: attacker deposits a minimal amount into each `DepositToken` offered by the pool (bounded by the pool's own deposit-token cap), then calls `msdTOKEN.transfer(victim, 1 wei)` for each. Each call appends to `depositTokensOfAccount[victim]`. After `30 - (victim's existing entries)` transfers, `deposit()` for any new collateral type, `DebtToken.issue`/`mint` for any new synthetic, `SmartFarmingManager.leverage`, and liquidation `seize` of a new collateral type to the victim all revert.
- Because a victim with open debt cannot transfer the dust (their `unlockedBalanceOf` is reduced by the debt via `debtPositionOf`), the griefed entries may be un-clearable, and crucially the victim cannot deposit *new* collateral to restore health when the market moves against them → forced liquidation of otherwise rescuable positions.

### Impact Explanation
Temporary-to-persistent freezing of a victim's ability to take new positions, and forced liquidations: a victim carrying debt near the health threshold cannot top up with a collateral type they don't already hold, so a price move that would normally be survivable becomes a liquidation. Impact is bounded by `maxLiquidable` per liquidation event but is repeatable. For debt-free victims the freeze is temporary (they can forward the dust), but for indebted victims the dust itself is locked and the set cannot be emptied.

### Likelihood Explanation
High feasibility for an unprivileged EOA: only requires dust balances of each listed `msdTOKEN` (acquired cheaply via `deposit`), and `transfer` is a public entry point with no recipient consent. Cost scales with the number of enabled collaterals (≤30) and is negligible in USD terms. Limitation: pools with few enabled deposit tokens may leave the victim room under the cap unless the attacker can also induce debt-token entries, which requires the victim to hold debt (debt tokens are non-transferable, so debt slots can't be forced — debt-side filling is limited to entries the victim creates via `SmartFarmingManager` `mint` flows where `to_` is chosen by the manager caller).

### Recommendation
- Only add a token to `depositTokensOfAccount` on mint/deposit for `onBehalfOf` (self-initiated accounting), or make `transfer`/`transferFrom` recipient registration opt-in.
- Alternatively, revert transfers to accounts whose list is full *and* let anyone call a `sweepDust`-style removal, or raise/cap via `deposit()`-only accounting and exclude unsolicited transfers from the cap.
- At minimum, exempt entries created by inbound transfers from counting toward `MAX_TOKENS_PER_USER`.

### Proof of Concept
```ts
// Hardhat fork test sketch
const victim = victimUser.address // holds debt, near liquidation threshold
for (const dt of await pool.getDepositTokens()) {
  const msd = await ethers.getContractAt('DepositToken', dt)
  // attacker acquires dust: deposit 1 wei underlying on own account
  await underlying.approve(msd.address, 1)
  await msd.deposit(1, attacker.address)
  await msd.transfer(victim, 1) // no revert; adds dt to depositTokensOfAccount[victim]
}
// depositTokensOfAccount[victim] + debtTokensOfAccount[victim] == 30 now
await expect(
  newDepositToken.connect(victim).deposit(amount, victim.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')
// victim cannot forward the dust: unlockedBalanceOf is reduced by debt
await expect(
  msd.connect(victimUser).transfer(attacker.address, 1)
).to.be.revertedWithCustomError(msd, 'NotEnoughFreeBalance')
// oracle price drops -> victim liquidated with no way to add collateral
```

Note: debt-side list entries cannot be forced by the attacker (`DebtToken.transfer` reverts with `TransferNotSupported`), so the strongest variant requires the pool to have enough enabled deposit tokens (or the victim to already hold some entries) for dust deposits alone to reach the 30-slot cap — on deployments with few collaterals the attack only partially fills the list.