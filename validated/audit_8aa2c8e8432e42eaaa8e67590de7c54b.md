### Title
Griefing via dust transfer of a DepositToken with a reverting oracle permanently freezes victim's ability to withdraw or transfer any collateral - (File: contracts/Pool.sol)

### Summary
`Pool.depositOf` iterates over every entry in the victim's per-account `depositTokensOfAccount` list and calls `masterOracle().quoteTokenToUsd` on each token's `underlying()` (`contracts/Pool.sol:274-288`). This feeds `Pool.debtPositionOf`, which feeds `DepositToken.unlockedBalanceOf`, which gates `withdraw`, `transfer`, and `transferFrom` via `_revertIfLocked` (`contracts/DepositToken.sol:180-182, 383-398`). Any unprivileged user can push an arbitrary deposit token into a victim's list by transferring them a dust amount (`_transfer` auto-adds tokens on first receipt, `contracts/DepositToken.sol:517-520`). If any deposit token's underlying oracle reverts (deprecated/disabled feed, malfunctioning vault-share oracle, etc.), every `debtPositionOf` call for that victim reverts, freezing all of their collateral — the same failure mode as SafEth's unremovable malfunctioning derivative DoS'ing `unstake`.

### Finding Description
Analog to the external report: instead of a global derivatives array DoS'ing `unstake`, Metronome has a per-account deposit-token list that cannot be cleaned without calling the very functions that revert.

Attack path (all unprivileged):
1. Attacker deposits a dust amount of collateral into a `DepositToken` whose `underlying()` has a malfunctioning/untrusted price source in `MasterOracle` (e.g., a `quoteTokenToUsd` implementation that reverts — a paused feed, a removed oracle route, or a reverting vault-share/vToken valuation). Attacker only needs enough to mint a nonzero msdTOKEN balance.
2. Attacker calls `DepositToken.transfer(victim, dust)`. `transfer` → `_transfer` → since `victim`'s prior balance is 0, `pool.addToDepositTokensOfAccount(victim)` adds the bad token to the victim's list (`contracts/DepositToken.sol:517-520`, `contracts/Pool.sol:216-220`).
3. Victim calls `withdraw(amount, to)` on an unrelated healthy `DepositToken`. `_revertIfLocked` → `unlockedBalanceOf` → `pool.debtPositionOf` → `depositOf` loops `depositTokensOfAccount` and calls `quoteTokenToUsd(badUnderlying, ...)` → reverts (`contracts/DepositToken.sol:406-411`, `contracts/Pool.sol:274-288`). Even with zero debt, `debtPositionOf` still evaluates `depositOf` fully.
4. Victim cannot even shed the bad token: `transfer`/`transferFrom` also call `_revertIfLocked` → `unlockedBalanceOf` → same revert. `withdraw` on the bad token itself also reverts. The per-account entry is only removed inside `_burn`/`_transfer`, both unreachable.

Governor-side cleanup does not reliably help: even if governance removes the DepositToken from `depositTokens`, it remains in the victim's `depositTokensOfAccount` list (removal happens only on balance→0 inside the token), and `withdraw` becomes unreachable anyway because `_withdraw` is gated by `onlyIfDepositTokenExists` (`contracts/DepositToken.sol:540`). There is no admin function to purge an account's token list.

### Impact Explanation
Permanent freezing of user funds: a single forced list entry with a reverting oracle bricks `withdraw`, `transfer`, `transferFrom`, and any `debtPositionOf`-dependent flow (including `Pool.liquidate`/`swap` health checks via `unlockedBalanceOf` and debt-position queries) for the victim across **all** collaterals, not just the malicious token. This matches the accepted impact class (permanent/temporary freezing of funds).

### Likelihood Explanation
Requires one registered deposit token whose `underlying()` price quote reverts. Metronome integrates vault-share and multi-source oracles via `MasterOracle` (`contracts/interfaces/external/IMasterOracle.sol`), where a missing/stale/decommissioned route or a reverting Vesper vToken price per share is a realistic malfunction — precisely the "malfunctioning external dependency" condition of the source report. The attacker action itself is a cheap dust deposit + transfer by any EOA. Caveat: I could not fully verify `MasterOracle`'s revert semantics from in-repo code (it is an external dependency); the finding assumes `quoteTokenToUsd` reverts on missing/failed price sources, which is the standard behavior for this oracle family.

### Recommendation
Make `depositOf`/`debtOf` resilient: wrap each per-token `quoteTokenToUsd` in a `try/catch` and treat failures as 0-valued collateral (and/or flag the position conservatively), so a single bad token cannot brick unrelated exits. Alternatively/additionally, allow `transfer`/`withdraw` to skip lock checks when the account has no debt (`_debtInUsd == 0` fast-path already exists in `unlockedBalanceOf` — reorder `debtPositionOf`/`depositOf` so debt is computed and checked before iterating deposits), and provide a governed/escape function to remove entries from `depositTokensOfAccount`.

### Proof of Concept
```solidity
// Foundry fork test sketch
// Setup: pool with depositTokenGood (USDC) and depositTokenBad whose
// underlying's MasterOracle.quoteTokenToUsd reverts (mock via vm.mockCallRevert
// on the oracle, or point oracle to a token with no configured feed).

// 1. Attacker seeds dust position in the bad token
ERC20(badUnderlying).approve(address(depositTokenBad), dust);
depositTokenBad.deposit(dust, attacker);

// 2. Attacker forces bad token into victim's account list
depositTokenBad.transfer(victim, dust);
assertEq(pool.getDepositTokensOfAccount(victim)[1], address(depositTokenBad));

// 3. Victim tries to withdraw GOOD collateral -> reverts inside depositOf loop
vm.prank(victim);
vm.expectRevert(); // quoteTokenToUsd reverts on bad underlying
depositTokenGood.withdraw(victimBalance, victim);

// 4. Victim cannot transfer the bad token away either -> permanently locked
vm.prank(victim);
vm.expectRevert();
depositTokenBad.transfer(attacker, dust);
```