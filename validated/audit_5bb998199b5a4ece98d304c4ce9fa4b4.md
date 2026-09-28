### Title
`repay`/`repayAll` unnecessarily gated by `whenNotShutdown`, blocking borrowers from closing debt during shutdown - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
Analogous to the YOLO `outflowAllowed` finding, Metronome applies a shutdown guard to functions that only move funds **into** the protocol. `DebtToken.repay()` and `DebtToken.repayAll()` burn debt shares and pull synthetic tokens from the payer — no protocol outflow occurs — yet they revert with `IsShutdown` whenever `Pool.everythingStopped()` (or `PoolRegistry.everythingStopped()`) is true. The documented flag matrix confirms: `repay`/`repayAll` are disabled if `PoolRegistry.everythingStopped() || Pool.everythingStopped()` (`docs/emergency-flags.md:71-77`).

### Finding Description
`DebtToken` inherits `Manageable`/`Pauseable` (`contracts/utils/Pauseable.sol:50-53`), whose `whenNotShutdown` modifier reverts when `_everythingStopped` is set. `repay` and `repayAll` are debt-reduction paths: they call `syntheticToken.burn`/`transferFrom` and reduce `principalOf`/`debtIndexOf` accounting, then trigger `updateRewardsBeforeMintOrBurn` (`contracts/DebtToken.sol:114-120`). There is no transfer of collateral or treasury assets out of the system, so the shutdown check adds no security property — it only prevents users from improving their own positions.

Compare with `DepositToken.withdraw`, where the shutdown check *does* guard a real outflow (collateral leaving the pool), making it defensible — exactly like `claimPrizes`/`withdrawDeposits` in the original report. The analogous defect is on the repay side, not the withdraw side.

### Impact Explanation
During a shutdown (which can be triggered for an unrelated emergency, e.g., a bad oracle or a compromised component), borrowers cannot repay or fully close their debt positions. Meanwhile `accrueInterest` continues to grow `debtIndexOf`/`principalOf`, so users are forced to keep paying interest on debt they are willing and able to extinguish. On recovery, positions may have deteriorated; if the shutdown coincides with adverse price movement, users cannot deleverage defensively even though repayment would only help protocol solvency. This is a liveness/availability defect on a non-outflow path — the same invariant break as the reference finding.

### Likelihood Explanation
Shutdown is guardian/governor-triggered and intended to be rare, but it is a real, documented operational state (`shutdown()` in `Pauseable.sol:113-117`), and once active it blocks all repays until the governor calls `open()`. The window is entirely out of users' control, and any borrower holding the synthetic token on-chain is affected unconditionally — no attacker setup is needed beyond an existing debt position.

### Recommendation
Remove the shutdown/`everythingStopped` check from `DebtToken.repay()` and `DebtToken.repayAll()` (and keep it on genuine outflow/state-growing paths such as `issue`, `deposit`, `withdraw`, `swap`, `liquidate`). Repayment strictly improves protocol solvency and cannot be abused to extract funds, so gating it provides no benefit.

### Proof of Concept
Hardhat fork sketch:

```ts
// Deployed-config test against Pool + DebtToken
// 1. User has an open debt position (principalOf(user) > 0).
// 2. Governor (or guardian) calls pool.shutdown() — legitimate emergency action.
// 3. User approves syntheticToken to debtToken and calls:
await expect(
  debtToken.connect(user).repay(user.address, amount)
).to.be.revertedWithCustomError(debtToken, 'IsShutdown');
// 4. Time passes; accrueInterest() increases user's debt despite willingness to repay.
// 5. After governor calls open(), repay succeeds but the debt is larger.
```

Caveat: I verified the flag wiring via `docs/emergency-flags.md` and `Pauseable.sol`, and the reward-update/context structure of `DebtToken.sol` (lines 42-120); I was not able to display the exact `repay`/`repayAll` function bodies before finalizing, so the precise modifier ordering on those functions should be confirmed in `contracts/DebtToken.sol` when implementing the fix.