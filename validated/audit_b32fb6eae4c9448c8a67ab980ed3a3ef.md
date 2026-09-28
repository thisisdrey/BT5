### Title
Attacker can permanently DoS deposits, withdrawals and liquidations by filling the `feeCollector`'s per-account token list via dust `DepositToken` transfers - ([File: contracts/DepositToken.sol](metronome-synth-public--005/contracts/DepositToken.sol))

### Summary
`DepositToken._transfer` unconditionally calls `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's balance goes from zero to non-zero. `Pool` enforces `MAX_TOKENS_PER_USER = 30` and reverts with `UserReachedMaxTokens` once the list is full. Because the recipient has no way to opt out, an attacker can dust-transfer small amounts of every existing `DepositToken` to the protocol's `feeCollector` (or any critical account). Once the cap is reached, every code path that moves a *new* token type into `feeCollector` reverts: deposit fees (`_mint` → `addToDepositTokensOfAccount`), withdraw fees (`_transfer` to `feeCollector`), liquidation fees (`seize` → `_transfer` to `feeCollector`), and repay fees (`SyntheticToken.seize` to `feeCollector` uses the same accounting for synthetic tokens only — the DepositToken paths are the affected ones). Since the `feeCollector` is a passive address that cannot call `transfer` to empty its list, the DoS is effectively permanent for the affected token types.

### Finding Description
In `DepositToken.sol`:

- `seize` → `_transfer` (lines 343–345, 498–526): on line 518–520, `_recipientBalanceBefore == 0 && amount_ > 0` triggers `pool.addToDepositTokensOfAccount(recipient_)`.
- `_mint` (lines 469–489): same logic on line 486–488.
- `_withdraw` (line 547) transfers the fee to `feeCollector` via `_transfer`; `deposit` (line 231) mints the fee to `feeCollector`; `Pool.liquidate` (Pool.sol lines 591–593) calls `depositToken_.seize(account_, feeCollector, _fee)`.

In `Pool.sol`, `addToDepositTokensOfAccount` enforces the `MAX_TOKENS_PER_USER = 30` cap (line 79) and reverts with `UserReachedMaxTokens` when the mapped set is full; `removeFromDepositTokensOfAccount` only clears an entry when the account's balance reaches zero — which requires `feeCollector` itself to initiate a transfer, something it cannot do.

Attack steps (unprivileged EOA):

1. Deposit a tiny amount of each of the pool's deposit-token collaterals to obtain unlocked `msd*` balances (or acquire via transfers).
2. Call `msdX.transfer(feeCollector, 1)` for every deposit token the `feeCollector` does not yet hold, until `depositTokensOfAccount[feeCollector].length == MAX_TOKENS_PER_USER`.
3. Thereafter, any call that would credit a *new* `DepositToken` type to `feeCollector` reverts:
   - `deposit()` reverts on the fee `_mint(feeCollector, _fee)` for a token the feeCollector doesn't hold yet (blocks onboarding/usage of newly added collaterals entirely).
   - `withdraw()`/`withdrawFrom()`/`flashWithdraw()` revert on the fee `_transfer` for such tokens.
   - `Pool.liquidate()` reverts on the fee `seize` for such tokens, blocking liquidation of underwater positions — a liveness/insolvency risk.

The attacker only needs unlocked dust (no debt), so `_revertIfLocked` passes; `nonReentrant` does not apply to `transfer`; there is no recipient opt-out or whitelist.

### Impact Explanation
- **Permanent freezing of new collateral types**: once the cap is filled, the first fee-bearing deposit of any newly listed collateral always reverts, so that collateral is permanently unusable while fees are enabled.
- **Temporary freezing of funds**: withdrawals and liquidations of affected token types revert whenever the fee path would credit a new token to `feeCollector`, locking user collateral and blocking bad-debt cleanup.
- Cost to the attacker is only dust deposits/transfers and gas; no privileged role, oracle manipulation, or governance action is required. This is the Metronome analog of CVE-2021-2208's "high-privileged → hang/crash (DoS)" class, but reachable by an unprivileged user.

### Likelihood Explanation
Feasibility depends on the number of distinct `DepositToken`s in the pool: filling 30 slots requires up to 30 listed collaterals. Even with fewer collaterals, the same primitive applies to `debtTokensOfAccount` if the attacker can cause new debt tokens to be credited (e.g., via `DebtToken.issue`/`flashIssue` on behalf of accounts where permitted) — worth verifying per deployment. Partial fills still degrade the `feeCollector` until only a few free slots remain, after which a single dust transfer completes the DoS. Likelihood is moderate on pools with many listed collaterals.

### Recommendation
- Do not revert in `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount` when the cap is hit for protocol-internal recipients; instead skip tracking (the cap exists to bound `debtPositionOf` iteration — the `feeCollector` never needs a health check).
- Alternatively, route fees through a dedicated `FeeCollector` contract that does not participate in the per-account mapped-set accounting, or exempt `feeCollector`/`Treasury` from list tracking.
- Also consider making `DepositToken.transfer` to a first-time recipient cap check non-reverting for addresses that are not position holders.

### Proof of Concept
Hardhat (fork/deployed config with `depositFee > 0` and ≥2 listed collaterals for demonstration; scale to all listed tokens to exhaust the cap):

```typescript
// setup: attacker deposits dust in each collateral to get unlocked msd* balance
const attacker = await ethers.getSigner(ATTACKER);
const feeCollector = await poolRegistry.feeCollector();

// 1) fill feeCollector's depositTokensOfAccount list
for (const msd of allDepositTokens) {
  const bal = await msd.balanceOf(attacker.address);
  await msd.connect(attacker).transfer(feeCollector, 1); // adds entry
}
// assert list is full
expect(await pool.depositTokensOfAccountLength(feeCollector)).to.eq(30);

// 2) any new depositToken type's fee mint to feeCollector now reverts
const newDepositToken = await deployAndAddDepositToken(); // governor adds new collateral
await underlying.connect(attacker).approve(newDepositToken.address, amount);
await expect(
  newDepositToken.connect(attacker).deposit(amount, attacker.address) // depositFee > 0
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
// deposit of a token feeCollector already holds still succeeds — the block is per *new* token type

// 3) liquidation fee path for such token also reverts
await expect(
  pool.connect(liquidator).liquidate(msSynth, victim, amountToRepay, newDepositToken.address)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Key assertions: `addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` inside `DepositToken._transfer`/`_mint` (DepositToken.sol:487, 519), and `feeCollector` has no mechanism to call `transfer` to remove entries, making the condition persistent.