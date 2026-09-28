### Title
Dust deposit-token stuffing permanently blocks victims from adding new collateral/debt positions via `MAX_TOKENS_PER_USER` - ([File: contracts/Pool.sol])

### Summary
Analogous to CVE-2017-1000407 (unprivileged flooding of a resource causes a panic/DoS), an unprivileged attacker can "flood" a victim's per-account token list by sending dust amounts of `DepositToken`s. Each first-time receipt calls `Pool.addToDepositTokensOfAccount`, and once the combined `debtTokensOfAccount + depositTokensOfAccount` count reaches `MAX_TOKENS_PER_USER` (30), every subsequent addition reverts with `UserReachedMaxTokens`, blocking the victim from depositing new collateral types, minting new synths, and receiving transfers/seizures into new tokens.

### Finding Description
`DepositToken._transfer` adds the token to the recipient's per-account list on any first-time nonzero receipt, with no minimum amount and no opt-in:

- `contracts/DepositToken.sol:518-520` — `if (_recipientBalanceBefore == 0 && amount_ > 0) pool.addToDepositTokensOfAccount(recipient_);`
- The same happens on minted deposit balances via `deposit(amount_, onBehalfOf_)` → `_mint` → `pool.addToDepositTokensOfAccount(account_)` (`contracts/DepositToken.sol:486-488`), where the attacker supplies `onBehalfOf_ = victim` and an arbitrary dust `amount_` (e.g., 1 wei of underlying; the treasury balance-delta check in `deposit` at lines 225-227 accepts it).
- `Pool.addToDepositTokensOfAccount` is gated only by `onlyIfAdditionWillNotReachMaxTokens`, which reverts once `debtTokensOfAccount.length + depositTokensOfAccount.length >= 30` (`contracts/Pool.sol:143-148, 216-220`).

Because there is no way for the victim to refuse inbound `transfer`/`transferFrom`/`deposit onBehalfOf`, the attacker fills the list unilaterally. Once full:

1. `DepositToken.deposit(..., victim)` for any collateral not already in the list reverts inside `_mint` → `addToDepositTokensOfAccount` → `UserReachedMaxTokens`.
2. `DebtToken.issue`/`mint` to the victim reverts for any synth not already in the list (`DebtToken._mint` → `pool.addToDebtTokensOfAccount` at `contracts/DebtToken.sol:597-600`), so the victim cannot open new debt positions.
3. `Pool.liquidate` → `DepositToken.seize` reverts when the liquidator or `feeCollector` would receive a token for the first time and their own lists are full — the attacker can also stuff the permissionless `feeCollector`? No (governor-set), but can stuff any liquidator EOA they choose to target, selectively breaking their liquidations.

If the victim already has debt such that `unlockedBalanceOf(victim) == 0` (`DepositToken.sol:383-398`), the dusted tokens are locked and cannot be transferred out, so the victim cannot free the slots themselves — the block is effectively permanent for the duration of the debt.

### Impact Explanation
Temporary/permanent freezing of position functionality and forced-liquidation risk: a victim holding debt whose collateral types aren't already in their list cannot deposit a new collateral type to restore health before liquidation, cannot mint new synth types, and cannot receive new deposit tokens. Combined with locked balances (unlocked = 0), the victim cannot remove the dust entries. This is a liveness/availability break of the `deposit`/`issue`/`seize` invariants for the targeted account, matching the kernel-panic DoS class: cheap, repeatable, unprivileged writes that trigger a revert (panic) for the victim.

### Likelihood Explanation
Requires only that the pool lists enough distinct deposit/debt tokens that the victim's remaining slots can be exhausted (the cap counts deposit + debt tokens together, so ~15 deposit + ~15 debt listings suffice, or fewer if the victim already holds positions). Cost is ~N dust transfers/deposits of 1 wei each plus gas — no privileged role, no oracle manipulation, no capital at risk. Any EOA or attacker contract can execute it via `deposit(1, victim)` or `transfer(victim, 1)` across every listed `DepositToken`, and by dust-issuing/transferring into `DebtToken` lists indirectly via `SmartFarmingManager` flows if needed.

### Recommendation
- Require recipient opt-in for list additions: only add on `deposit(..., onBehalfOf_)` when `onBehalfOf_ == _msgSender()` or via an explicit `acceptCollateral(token)` allowlist mapping; do not add on bare `transfer`/`seize` receipts, or let transfers settle without registering the token (track collateral via debtPositionOf over a fixed registry instead).
- Alternatively, allow a permissionless `removeFromDepositTokensOfAccount` callable by the account itself regardless of lock status (burn/skip dust entries), or raise/segment the cap so deposit and debt lists are counted separately.

### Proof of Concept
Hardhat-style sketch:

```ts
// victim already has debt in msUSD so unlockedBalanceOf(victim) == 0 for dusted tokens
const victim = alice.address
const depositTokens = await pool.getDepositTokens() // all listed collaterals

// 1) Attacker deposits 1 wei of each underlying on behalf of victim
for (const dt of depositTokens) {
  const token = await ethers.getContractAt('DepositToken', dt)
  const underlying = await ethers.getContractAt('IERC20', await token.underlying())
  await underlying.connect(attacker).approve(token.address, 1)
  await token.connect(attacker).deposit(1, victim) // mints dust, registers token for victim
}
// 2) Attacker also transfers dust of tokens it holds
for (const dt of heldTokens) {
  await token.connect(attacker).transfer(victim, 1)
}

// victim's list is now at MAX_TOKENS_PER_USER
expect(await pool.getDepositTokensOfAccount(victim)).to.have.lengthOf(/* slots filled */)

// 3) Victim tries to deposit a new collateral type to improve health -> reverts
await expect(
  newDepositToken.connect(victim).deposit(parseEther('100'), victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 4) Victim tries to mint a new synth type -> reverts in DebtToken._mint
await expect(
  newDebtToken.connect(victim).issue(parseEther('1'), victim)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 5) Victim tries to transfer dust out while fully locked -> reverts
await expect(
  dustedToken.connect(victim).transfer(attacker.address, 1)
).to.be.revertedWithCustomError(dustedToken, 'NotEnoughFreeBalance')
```

Key code paths: `Pool.onlyIfAdditionWillNotReachMaxTokens` (`contracts/Pool.sol:143-148`), `Pool.addToDepositTokensOfAccount` (`contracts/Pool.sol:216-220`), `DepositToken._transfer` list insertion (`contracts/DepositToken.sol:517-525`), `DepositToken._mint` list insertion (`contracts/DepositToken.sol:485-488`), `DebtToken._mint` list insertion (`contracts/DebtToken.sol:597-600`), and `unlockedBalanceOf` locking (`contracts/DepositToken.sol:383-398`).