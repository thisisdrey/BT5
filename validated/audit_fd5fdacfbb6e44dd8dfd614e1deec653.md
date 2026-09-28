Let me check the swap and SmartFarmingManager surfaces for rounding-to-zero reverts reachable by an attacker affecting others.Let me verify the `MAX_TOKENS_PER_USER` / `addToDepositTokensOfAccount` griefing path and `DepositToken.deposit` signature.### Title
Attacker can permanently DoS synthetic issuance by shrinking remaining `DebtToken` supply headroom below `debtFloorInUsd` - ([File: contracts/DebtToken.sol])

### Summary
Analogous to MuteBond's "payout < min-lock mints 0 → deposit always reverts", Metronome has a two-sided bound on every debt mint: `_mint` reverts with `DebtLowerThanTheFloor` when the resulting debt is below `debtFloorInUsd`, and reverts with `SurpassMaxDebtSupply` when `totalSupply_ + amount_ > maxTotalSupply`. An unprivileged attacker can size their own issuance so the remaining supply headroom is smaller than the minimum viable mint, making *every* subsequent `issue`, `mint`, `flashIssue`, `Pool.swap` (synthetic mint side) and `SmartFarmingManager.leverage` for that debt token revert.

### Finding Description
`DebtToken._mint` enforces both bounds atomically:

```solidity
// contracts/DebtToken.sol:583-591
if (
    _debtFloorInUsd > 0 &&
    masterOracle_.quoteTokenToUsd(address(syntheticToken), _balanceBefore + amount_) < _debtFloorInUsd
) {
    revert DebtLowerThanTheFloor();
}

totalSupply_ += amount_;
if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply();
```

Both `debtFloorInUsd` (on `Pool`) and `maxTotalSupply` (on `DebtToken`) are live on deployed pools. If `floorTokens = quoteUsdToToken(syntheticToken, debtFloorInUsd)` and the attacker issues debt until `totalSupply_ > maxTotalSupply - floorTokens`, then:

- Any mint with `amount_ >= floorTokens` (per-account, assuming fresh account) exceeds `maxTotalSupply` → `SurpassMaxDebtSupply`.
- Any mint with `amount_ < floorTokens` → `DebtLowerThanTheFloor`.

So no account can mint at all. Note the floor check is against `_balanceBefore + amount_`, so existing holders above the floor can still mint small top-ups — but any *new* borrower, and any swap mint hitting `SyntheticToken.maxTotalSupply`, is fully blocked. The same gap exists for `DepositToken._mint` (`SurpassMaxDepositSupply`) combined with dust deposits, though without a floor the minimum viable deposit is 1 wei, so the debt-token side is the exploitable one.

### Impact Explanation
Liveness: issuance for the targeted synthetic token is DoS'd for all new positions as long as the attacker keeps their debt open (or until governance raises `maxTotalSupply`/lowers the floor — same remediation profile as the original MuteBond report). `Pool.swap` into that synthetic also reverts since `syntheticTokenOut_.mint` respects `SyntheticToken.maxTotalSupply`, and `SmartFarmingManager.leverage` reverts inside `flashIssue`/`mint`. No funds are stolen; existing users can still repay and withdraw, matching the "medium" DoS profile of the source finding.

### Likelihood Explanation
Requires `debtFloorInUsd > 0` (deployed config on several pools) and enough collateral to bring `totalSupply_` within `floorTokens` of `maxTotalSupply` — capital cost on the order of `maxTotalSupply / collateralFactor`, comparable to the MuteBond attacker buying the epoch's remaining capacity. Fully unprivileged: `DebtToken.issue(amount_, onBehalfOf_)` is public and only gated by `onlyIfSyntheticTokenExists`, `whenNotShutdown`, `nonReentrant`, and the caller's own health check.

### Recommendation
When checking `SurpassMaxDebtSupply`, clamp: if the remaining headroom `maxTotalSupply - totalSupply_` is below `floorTokens`, either allow mints that exactly fill the cap or treat remaining headroom `< floorTokens` as effectively "cap reached" in quotes/UI; more robustly, auto-reduce `amount_` is not viable, so document that governance should raise `maxTotalSupply` or reset the epoch-equivalent (deploy new debt token) — mirroring the original recommendation "start a new epoch if maxDeposit() is smaller than a threshold".

### Proof of Concept
Hardhat (analogous to the provided `bonds.ts` test, in `test/DebtToken.test.ts` style):

```ts
it('DebtToken issuance DoS via cap-floor gap', async function () {
  // given: floor = $10,000 msUSD; cap set just above current supply
  await poolMock.updateDebtFloor(parseEther('10000'))
  const floorTokens = parseEther('10000') // msUSD ≈ $1
  const current = await msUSDDebt.totalSupply()
  // attacker deposits collateral and issues until headroom < floorTokens
  const headroomTarget = floorTokens.sub(1)
  await msUSDDebt.updateMaxTotalSupply /* governor step in test */ // not needed on fork; attacker just mints
  // attacker mints:
  const attacker = whale // EOA with collateral deposited via DepositToken.deposit
  const toIssue = (await msUSDDebt.maxTotalSupply()).sub(current).sub(headroomTarget)
  await msUSDDebt.connect(attacker).issue(toIssue, attacker.address)

  // then: every fresh-account mint reverts
  await expect(
    msUSDDebt.connect(user2).issue(floorTokens, user2.address)
  ).to.be.revertedWithCustomError(msUSDDebt, 'SurpassMaxDebtSupply')
  await expect(
    msUSDDebt.connect(user2).issue(floorTokens.sub(1), user2.address)
  ).to.be.revertedWithCustomError(msUSDDebt, 'DebtLowerThanTheFloor')
})
```

Key code paths: `_mint` floor + cap checks in `contracts/DebtToken.sol:583-591`, public entry `issue`/`mint`/`flashIssue`, and the same revert propagating through `Pool.swap` (`contracts/Pool.sol:668`) and `SmartFarmingManager.leverage` (`contracts/SmartFarmingManager.sol:191-192`).