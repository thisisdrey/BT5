### Title
Attacker can permanently DoS deposits, withdrawals, and liquidations by filling the fee collector's `depositTokensOfAccount` list to `MAX_TOKENS_PER_USER` via dust `DepositToken` transfers - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
The `content` advisory describes a class where an unhandled error on adversary-controlled input propagates all the way up and kills the service. The Metronome analog is `Pool.onlyIfAdditionWillNotReachMaxTokens`: any `DepositToken` mint/transfer that creates a first-time balance for an account calls `Pool.addToDepositTokensOfAccount`, which reverts with `UserReachedMaxTokens` once the account holds 30 distinct debt+deposit token entries. Because every fee-paying deposit, withdrawal, and liquidation mints/transfers msdTOKEN to the protocol `feeCollector`, an unprivileged attacker can fill the fee collector's list with dust transfers and make all fee-bearing protocol operations revert unconditionally.

### Finding Description
`DepositToken._transfer` and `DepositToken._mint` call `pool.addToDepositTokensOfAccount(recipient_)` whenever the recipient's prior balance is zero (`DepositToken.sol:486-488, 518-520`). `Pool.addToDepositTokensOfAccount` enforces `debtTokensOfAccount.length(account_) + depositTokensOfAccount.length(account_) >= MAX_TOKENS_PER_USER` (30) and reverts otherwise (`Pool.sol:143-148, 216-220`).

The revert propagates uncaught through every caller:

- `deposit` mints `_fee` to `pool.feeCollector()` (`DepositToken.sol:229-232`).
- `_withdraw` transfers `_fee` to `pool.feeCollector()` (`DepositToken.sol:545-548`), so `withdraw`/`withdrawFrom`/`flashWithdraw` all revert.
- `Pool.liquidate` → `DepositToken.seize` → `_transfer` of the protocol-fee portion to `feeCollector` reverts, blocking liquidations.
- `RecurringAirdrop`-style flows aside, even plain `transfer`/`transferFrom` to a victim at the cap reverts, blocking receipt of new collateral types.

Attack path (all public, unprivileged):
1. For each registered `DepositToken` i, attacker calls `deposit(dust, attacker)` then `transfer(feeCollector, dust)`. Each first-time transfer pushes a new entry into `depositTokensOfAccount[feeCollector]`.
2. `addDepositToken` allows up to `MAX_TOKENS_PER_USER` (30) deposit tokens (`Pool.sol:698-709`), so an attacker can fill all 30 slots for the fee collector.
3. Thereafter every operation that would create a *new* feeCollector balance reverts with `UserReachedMaxTokens`. Since fees continually route to `feeCollector` for each deposit token, once the list is full deposits (fee mint), withdrawals (fee transfer), and liquidations (seize fee) for any token not already in the list revert permanently — the governor cannot remove a deposit token with `totalSupply > 0` (`Pool.sol:737-738`), so recovery requires a contract upgrade.

Note `removeFromDepositTokensOfAccount` only triggers when the balance returns to zero, and the fee collector never spends its msdTOKEN balances, so the dust entries are effectively permanent.

### Impact Explanation
Temporary-to-permanent freezing of user funds and liquidation liveness failure: all withdrawals carrying a `withdrawFee` for tokens not yet in the fee collector's list revert, and liquidations revert once the cap is hit, exposing the pool to bad debt accrual. Funds already deposited cannot be exited through the normal `withdraw` path, satisfying the "temporary freezing of funds" / liveness invariant break.

### Likelihood Explanation
Fully permissionless once a pool has enough registered deposit tokens for the attacker to reach the combined 30-entry cap (plus any debt-token entries the fee collector incidentally holds). No privileged role, oracle manipulation, or malicious LayerZero component is required — only standard `deposit`/`transfer` calls, or `Operator.execute` batching them.

### Recommendation
Do not let fee minting/seizing to `feeCollector` or first-time-recipient bookkeeping revert user operations: wrap `pool.addToDepositTokensOfAccount` in a try/catch or skip the accounting entry for `feeCollector`, and cap/dedust by tracking fee balances in a separate mapping. Alternatively make fee accrual use a balance delta that doesn't create new set entries (e.g., accrue to an existing entry).

### Proof of Concept
```ts
// Hardhat (repo's existing test harness, test/Pool.test.ts style)
// given: pool with N registered DepositTokens (up to MAX_TOKENS_PER_USER=30)
const max = (await pool.MAX_TOKENS_PER_USER()).toNumber()
const feeCollector = await poolRegistry.feeCollector()

// 1. Attacker fills feeCollector's deposit token list with dust
for (const msd of depositTokens) {
  await underlying.mint(attacker.address, 10)
  await underlying.connect(attacker).approve(msd.address, 10)
  await msd.connect(attacker).deposit(10, attacker.address)      // mints fee -> feeCollector
  await msd.connect(attacker).transfer(feeCollector, 1)          // adds entry if missing
}
expect(await pool.getDepositTokensOfAccount(feeCollector)).to.have.lengthOf(max)

// 2. Any new deposit token's first fee mint to feeCollector reverts
const tx = msdNew.connect(alice).deposit(parseUnits('100', 6), alice.address)
await expect(tx).revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 3. Withdrawals for tokens not already in feeCollector's list revert
const tx2 = msdNew.connect(alice).withdraw(amount, alice.address)
await expect(tx2).revertedWithCustomError(pool, 'UserReachedMaxTokens')

// 4. Liquidations revert when seizing protocol fee to feeCollector
const tx3 = pool.connect(liquidator).liquidate(msEth.address, bob.address, repay, msdNew.address)
await expect(tx3).revertedWithCustomError(pool, 'UserReachedMaxTokens')
```