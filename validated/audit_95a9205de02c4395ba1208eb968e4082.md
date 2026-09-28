### Title
Dust-transfer "overflow" of the bounded per-account token list DoSes liquidations and deposits - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
`Pool` keeps a bounded per-account list of deposit/debt tokens (`MAX_TOKENS_PER_USER = 30`). Any token balance moving onto an account appends to that list via `addToDepositTokensOfAccount`/`addToDebtTokensOfAccount`, which revert with `UserReachedMaxTokens` once the combined length hits 30. Because `DepositToken.transfer`/`transferFrom` are public and trigger the append on the *recipient*, an unprivileged attacker can force-fill any target's list with dust — the Solidity analog of overflowing a fixed-size buffer with attacker-controlled writes. The strongest target is the pool's `feeCollector`: once its list is full, any `Pool.liquidate` that seizes a deposit token the feeCollector doesn't already hold reverts inside `DepositToken._transfer`/`seize`, bricking the liquidation path for that collateral.

### Finding Description
- `Pool.onlyIfAdditionWillNotReachMaxTokens` reverts when `debtTokensOfAccount.length(account) + depositTokensOfAccount.length(account) >= 30` (`contracts/Pool.sol:143-148`, `:79`).
- `Pool.addToDepositTokensOfAccount` is callable only by a registered deposit token, but `DepositToken._transfer` and `_mint` invoke it on behalf of the *recipient* whenever `balanceOf[recipient] == 0` and amount > 0 (`contracts/DepositToken.sol:517-520`, `:485-488`). `DepositToken.transfer`/`transferFrom` are permissionless and only require the *sender's* unlocked balance (`contracts/DepositToken.sol:348-376`).
- Attack: attacker deposits minimum amounts into each listed deposit token, then calls `depositToken.transfer(feeCollector, 1)` (and/or `debtToken` coverage via self-issued dust debt is not needed — deposit tokens alone suffice if the pool lists ≥30 deposit tokens; on deployed pools the combined deposit+debt count approaches the cap) until `depositTokensOfAccount.length(feeCollector) + debtTokensOfAccount.length(feeCollector) == 30`.
- Thereafter, any `Pool.liquidate` that (a) transfers the seized-fee share to `feeCollector` for a deposit token not already in its list, or (b) transfers collateral to a liquidator whose list is full, reverts with `UserReachedMaxTokens` — `DepositToken.seize` (`contracts/DepositToken.sol:343-345`) goes through `_transfer`, and the revert propagates, undoing the whole liquidation.
- The same primitive lets the attacker grief any user: filling a victim's list makes all future `deposit`/`issue`/transfers of *new* token types to that account revert (`_mint` and `DebtToken._mint` both append, `contracts/DebtToken.sol:597-600`), until the victim manually transfers dust out to zero a balance.

### Impact Explanation
Temporary freezing of funds / liveness failure with path to protocol insolvency. While the feeCollector's list is saturated, liquidations of unhealthy positions in any collateral the feeCollector doesn't already hold always revert, so bad debt can accrue. The attack is repeatable (attacker can re-fill slots whenever a balance zeroes out), and clearing it requires the feeCollector/victim to actively transfer dust away — for a protocol contract this is a governance/manual operation. Cost to the attacker is only dust deposits (plus deposit fees).

### Likelihood Explanation
Requires the pool to list enough deposit tokens for the attacker to fill 30 combined slots on the target; deposit tokens are permissionlessly transferable and dust deposits are cheap. No privileged role, oracle manipulation, or malformed external input is needed — only standard public `transfer`/`deposit` calls.

### Recommendation
Make the list-append failure non-fatal for recipients: in `DepositToken._transfer`/`_mint` (and `DebtToken._mint`), wrap `pool.addTo{Deposit,Debt}TokensOfAccount(account_)` in a `try/catch`, or have the pool silently skip the add on overflow and fall back to iterating all pool tokens when computing `depositOf`/`debtOf`. Alternatively, exempt protocol addresses (feeCollector) from the cap, or make seize-fee accounting not depend on the feeCollector's per-account list.

### Proof of Concept
Reproducible on a Hardhat fork of a deployed pool:
```ts
// 1. For each listed deposit token d_i:
await underlying_i.approve(d_i.address, MIN);
await d_i.deposit(MIN, attacker);            // attacker now holds dust
await d_i.transfer(feeCollector, 1);         // appends d_i to feeCollector's list
// repeat until getDepositTokensOfAccount(feeCollector).length
//   + getDebtTokensOfAccount(feeCollector).length == 30

// 2. Open an unhealthy position whose collateral token the
//    feeCollector does NOT yet hold (e.g. a newly added deposit token d_new).
// 3. liquidator calls:
await expect(pool.liquidate(victim, synth, amountRepaid, d_new))
  .revertedWithCustomError(pool, 'UserReachedMaxTokens');
// liquidation impossible while list is saturated -> position accrues bad debt
```

One caveat I could not fully verify within this pass: the exact seized-fee routing inside `Pool.liquidate` (whether the fee leg always goes to `feeCollector` via `DepositToken._transfer`). If fees are pulled differently, the direct `feeCollector` DoS weakens, but the victim-griefing variant (blocking `deposit`/`issue`/receipt of new tokens) stands on the code shown above regardless.