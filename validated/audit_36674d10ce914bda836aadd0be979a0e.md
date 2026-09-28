### Title
Dust deposits fill a victim's (or the feeCollector's) token list to `MAX_TOKENS_PER_USER`, reverting all subsequent deposits/withdrawals - (File: contracts/Pool.sol)

### Summary
`Pool.addToDepositTokensOfAccount` reverts with `UserReachedMaxTokens` once an account's combined debt+deposit token list reaches `MAX_TOKENS_PER_USER = 30` (`contracts/Pool.sol:79,143-148,216-220`). Because `DepositToken.deposit(amount_, onBehalfOf_)` lets anyone mint msdTokens to an arbitrary recipient (`contracts/DepositToken.sol:211-237`, `_mint` → `addToDepositTokensOfAccount` at line 487), an unprivileged attacker can dust-fill any target's list. The same mechanism applies to `_transfer` (line 519), which is invoked on every fee-bearing `deposit`/`withdraw`/`seize` toward the `feeCollector`. This is the on-chain analog of CVE-2015-8630's null-input crash: an attacker-supplied input drives a shared accounting structure into a state where normal operations revert, freezing funds.

### Finding Description
Attack path (fully unprivileged, no privileged roles, no fake contracts needed):

1. For each listed `DepositToken` in the pool, attacker calls `depositToken.deposit(1 wei_of_underlying, victim)`. Each call mints msdTokens to `victim`; since `balanceOf[victim]` was 0, `pool.addToDepositTokensOfAccount(victim)` runs and appends the token (`DepositToken.sol:485-488`).
2. `depositTokens.values` is capped at 30 by `addDepositToken` (`Pool.sol:703`), so after ~30 dust deposits the victim's `depositTokensOfAccount` list is full.
3. From then on, every code path that adds a *new* token to the victim's list reverts: `deposit(amount, victim)` for any collateral the victim doesn't yet hold, `transfer`/`transferFrom` to the victim, and `seize(account, victim, ...)` if the victim acts as liquidator.

The more severe variant targets `feeCollector`:

- `_mint(_pool.feeCollector(), _fee)` in `deposit` (`DepositToken.sol:231`) and `_transfer(account_, _pool.feeCollector(), _fee)` in `_withdraw` (`DepositToken.sol:547`) both add the token to the feeCollector's list when its prior balance is 0.
- Attacker dust-fills the feeCollector's list the same way (the feeCollector cannot opt out; `deposit` to `onBehalfOf_ = feeCollector` is unrestricted — only the Treasury is blocked at line 223).
- After that, **every** `deposit` with `depositFee > 0` of a collateral the feeCollector has never received reverts inside `_mint`, and **every** `withdraw`/`withdrawFrom`/`flashWithdraw`/`liquidate` fee transfer of such a collateral reverts inside `_transfer` — i.e., pool-wide DoS of deposits and withdrawals for those markets. The invariant that breaks is liveness/exit: users cannot withdraw collateral because the fee leg of the withdraw always reverts.

Checks that do not mitigate it: `whenNotPaused`/`nonReentrant`/`onlyIfDepositTokenExists` are orthogonal; `_revertIfLocked` only constrains the sender; `SynthContext` meta-sender checks don't apply to a direct EOA call; `MAX_TOKENS_PER_USER` itself is the revertsource.

### Impact Explanation
Temporary freezing of funds and denial of service. For a targeted victim, all new-collateral deposits and inbound msdToken transfers revert. For the feeCollector variant, deposits and withdrawals of affected collateral markets revert for *all* users until governance intervenes (e.g., `updateTreasury`/feeCollector rotation or the feeCollector emptying dust positions via `removeFromDepositTokensOfAccount` triggered by a zero-balance transfer). Withdrawals being bricked meets the "temporary freezing of funds" acceptance bar; cost to the attacker is dust × N tokens plus gas.

### Likelihood Explanation
High. Requirements: attacker EOA, dust amounts of listed underlyings, and a fee configuration where `depositFee`/`withdrawFee` > 0 for the feeCollector variant (or simply targeting any victim directly). No privileged actor, oracle manipulation, or flash capital is needed. It is reversible by the victim emptying a slot (burning a dust balance to 0 triggers `removeFromDepositTokensOfAccount`), which keeps severity at DoS rather than permanent loss — but victims holding locked collateral backing debt cannot reduce those balances, so their slot-removal ability is constrained. Caveat: if a prior audit already documented this `MAX_TOKENS_PER_USER` dust-griefing vector, it would fall under the "known issues" exclusion — that could not be verified from the indexed code.

### Recommendation
- In `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount`, do not revert on `MAX_TOKENS_PER_USER` for unsolicited receipts, or skip list-tracking for `seize`/fee mints to `feeCollector` (feeCollector accounting doesn't need the per-user list).
- Alternatively, allow anyone to call a `sweepDust`/forced-removal for sub-`depositFloor` balances, or exempt `feeCollector` from the cap.
- At minimum, reject `onBehalfOf_` deposits of amounts below a USD floor (`quoteTokenToUsd(amount) < dustThreshold`) so filling 30 slots costs real capital.

### Proof of Concept
Hardhat fork sketch (mainnet fork, real `pool`, `msdWETH` etc.):

```ts
const depositTokens = await pool.getDepositTokens(); // up to 30
for (const dt of depositTokens) {
  const token = await ethers.getContractAt('DepositToken', dt);
  const underlying = await ethers.getContractAt('IERC20', await token.underlying());
  await setBalance(underlying, attacker, 1); // or buy dust
  await underlying.connect(attacker).approve(token.address, 1);
  await token.connect(attacker).deposit(1, feeCollector); // or victim
}
expect(await pool.getDepositTokensOfAccount(feeCollector)).to.have.length(30);

// DoS: any deposit of a collateral the feeCollector has zero balance of reverts
await expect(
  msdNewCollateral.connect(alice).deposit(amount, alice) // _mint(feeCollector, fee) -> addToDepositTokensOfAccount -> UserReachedMaxTokens
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');

// DoS: withdraw whose fee leg transfers a token new to feeCollector reverts
await expect(
  msdNewCollateral.connect(alice).withdraw(balance, alice)
).to.be.revertedWithCustomError(pool, 'UserReachedMaxTokens');
```

Key code under test: `Pool.onlyIfAdditionWillNotReachMaxTokens` (`Pool.sol:143-148`), `addToDepositTokensOfAccount` (`Pool.sol:216-220`), `DepositToken._mint` hook (`DepositToken.sol:486-488`), fee mint in `deposit` (`DepositToken.sol:230-232`), fee transfer in `_withdraw` (`DepositToken.sol:546-548`).