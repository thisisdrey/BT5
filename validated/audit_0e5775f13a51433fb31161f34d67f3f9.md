### Title
`DebtToken.issue` bypasses the synthetic-token active check enforced everywhere else, allowing debt/synth issuance against a deactivated synthetic token - (File: contracts/DebtToken.sol)

### Summary
The analog of CVE-2021-3450 (a later check silently superseding an earlier security check) is the modifier composition on `DebtToken.issue()`. Issuance of synthetic assets is guarded by two independent flags: `SyntheticToken.isActive` and `DebtToken.isActive`. Every mint path is supposed to enforce the synthetic-active flag, but `issue()` only applies `onlyIfSyntheticTokenExists` and relies on `_mint()`, which enforces only `onlyIfDebtTokenIsActive`. The result: the "is the synthetic token active" check is never performed on the `issue` path — it is effectively clobbered by the later debt-token check inside `_mint`, exactly mirroring how the strict-flag check overwrote the CA check in OpenSSL.

### Finding Description
- `issue()` applies `whenNotShutdown`, `nonReentrant`, `onlyIfSyntheticTokenExists` — but **not** `onlyIfSyntheticTokenIsActive` (contracts/DebtToken.sol:235-245).
- `mint()` (SmartFarmingManager path) does apply `onlyIfSyntheticTokenIsActive` (contracts/DebtToken.sol:328-339), proving the flag is intended to gate debt creation.
- The shared `_mint()` helper enforces only `onlyIfDebtTokenIsActive` (contracts/DebtToken.sol:577), so when reached via `issue()` the synthetic-active check is skipped entirely — the second check replaces the first instead of being combined with it.
- `SyntheticToken` supports deactivation via `toggleIsActive()` (contracts/SyntheticToken.sol:120-123 shows `isActive` and the `SyntheticIsInactive` revert), which is the intended kill switch for issuance/minting of that asset.

Attack path (unprivileged EOA):
1. Governor deactivates a synthetic token (e.g., `msX.toggleIsActive()`) in response to a compromised/oracle-broken asset — `isActive = false`.
2. Attacker deposits collateral, then calls `debtToken.issue(amount, attacker)` directly (or via `Operator.execute`).
3. `onlyIfSyntheticTokenExists` passes (the token still exists in the pool), `_mint` passes (`isActive` on the *debt* token is still true), and `syntheticToken.mint(to_, _issued)` succeeds because `SyntheticToken.mint` is gated by `onlyIfCanMint` (caller is the DebtToken), not by `isActive`.
4. Attacker holds newly minted synthetic tokens backed by the deactivated asset and can dump them via `Pool.swap` into healthy synths or use them in `liquidate`/`repay`.

### Impact Explanation
Protocol insolvency / theft of user funds. The `isActive` flag on `SyntheticToken` is the emergency mechanism to halt expansion of a compromised or mispriced synthetic asset. Because `issue()` skips the check, an attacker can keep inflating supply of a deactivated (e.g., oracle-manipulated or deprecated) synth and swap it for healthy synthetic assets through `Pool.swap`, draining value from the pool at other depositors' expense. The invariant "deactivated synthetic ⇒ no new supply" is broken.

### Likelihood Explanation
Reachable by any unprivileged account with collateral whenever a synthetic token is deactivated while its debt token remains active (the flags are toggled independently — `SyntheticToken.toggleIsActive` vs `DebtToken.toggleIsActive`). Note: the two flags live on different contracts, so deactivation of a synth during an incident does not automatically deactivate its debt token, leaving the window open. Likelihood is moderate — it depends on a deactivation event occurring — but requires no privileged role for the exploit itself.

### Recommendation
Add `onlyIfSyntheticTokenIsActive` to `DebtToken.issue()` (and audit `repay`/`repayAll` for whether repaying a deactivated synth should remain allowed — repaying should stay permitted to let users unwind). Alternatively, consolidate the checks inside `_mint` so no call path can drop a check, i.e., make `_mint` enforce `onlyIfSyntheticTokenIsActive` as well as `onlyIfDebtTokenIsActive`.

### Proof of Concept
Hardhat test sketch (extends the existing `test/DebtToken.test.ts` pattern):

```ts
it('issue() succeeds even when the synthetic token is inactive', async function () {
  // given: alice has collateral deposited
  await met.connect(alice).approve(msdMET.address, MaxUint256)
  await msdMET.connect(alice).deposit(parseEther('6000'), alice.address)

  // governor deactivates the SYNTHETIC token only (debt token stays active)
  await msUSD.connect(governor).toggleIsActive()
  expect(await msUSD.isActive()).false
  expect(await msUSDDebt.isActive()).true

  // when: attacker issues against the deactivated synth — this should revert
  const tx = msUSDDebt.connect(alice).issue(parseEther('1'), alice.address)

  // then: currently SUCCEEDS — demonstrating the dropped check.
  // (mint() in the same conditions reverts with SyntheticIsInactive)
  await expect(tx).to.emit(msUSDDebt, 'SyntheticTokenIssued')
})
```

Note: this analysis is based on the indexed contract sources. I verified the modifier sets on `issue`, `mint`, `flashIssue`, and `_mint`, and that `SyntheticToken.mint` does not itself enforce `isActive`; I could not execute the PoC in this environment, so compiling and running the test against a fork/local chain is needed to confirm end-to-end.