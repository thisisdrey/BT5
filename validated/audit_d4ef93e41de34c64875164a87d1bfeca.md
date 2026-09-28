### Title
Borrower can keep interest permanently at zero by calling `accrueInterest()` every block, resetting `lastTimestampAccrued` while accrual rounds to zero - (File: contracts/DebtToken.sol)

### Summary
`DebtToken.accrueInterest()` is a permissionless public function. It unconditionally sets `lastTimestampAccrued = block.timestamp` at `contracts/DebtToken.sol:167`, even when the accrued interest rounds down to zero. Because the interest amount is computed as `wadMul(interestRatePerSecond * delta, totalSupply_)` (`contracts/DebtToken.sol:560-563`) with `wadMul` rounding half-up to 18 decimals (`contracts/lib/WadRayMath.sol:25-31`), a borrower can call `accrueInterest()` once per block so that each delta is only a few seconds and the product `interestRateToAccrue * totalSupply_` stays below the `0.5e18` rounding threshold. The timestamp still advances, so the un-accrued interest is lost forever rather than deferred — the exact same bug class as the Surge `getCurrentState()` finding.

### Finding Description
In `_calculateInterestAccrual` (`contracts/DebtToken.sol:551-566`):

```solidity
uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
if (_interestRateToAccrue > 0) {
    _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
    _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
}
```

`accrueInterest()` then does:

```solidity
if (block.timestamp == _lastTimestampAccrued) return;
lastTimestampAccrued = block.timestamp;
if (_interestAmountAccrued > 0) { ... }
```

If `wadMul` returns 0 for both the interest amount and the index delta, the code still commits `lastTimestampAccrued = block.timestamp`. The elapsed seconds are consumed without accruing anything. Repeating this every block keeps `debtIndex` and `totalSupply_` frozen while `balanceOf()` (which derives debt as `principal * debtIndex / debtIndexOf`, `contracts/DebtToken.sol:196-206`) shows no interest growth.

Rounding-to-zero threshold: `wadMul` returns 0 when `a * b < 0.5e18`. With a 10% APR, `interestRatePerSecond ≈ 3.17e9`. Per 12-second mainnet block, `_interestRateToAccrue ≈ 3.8e10`, so accrual rounds to zero whenever `totalSupply_ < ~1.3e7` wei. For a low-decimal synthetic like msBTC (8 decimals) on a 2-second L2 (Base/Optimism), the threshold is `~7.9e7` wei ≈ 0.79 msBTC — a realistic total-debt level for a small or new pool. The attack does not require small individual debt; it suppresses interest on the *global* `totalSupply_`/`debtIndex`, so every borrower in that DebtToken benefits.

Reachability: `accrueInterest()` has no `onlyPool`, pause, or reentrancy restriction — it is callable by any EOA or contract at zero cost besides gas. No other mitigation applies: `balanceOf`, `issue`, `repay` all reuse `_calculateInterestAccrual` against the same `lastTimestampAccrued`, so once the timestamp is advanced, the skipped interest is unrecoverable.

### Impact Explanation
Direct theft of protocol yield / insolvency vector: all borrowers' debt grows at 0% instead of `interestRate`, and the protocol permanently loses the interest that would have been minted to `feeCollector` (`contracts/DebtToken.sol:174`). The loss equals the full APR for the entire duration the caller maintains the grief loop, and it cannot be recovered retroactively because the elapsed time was already committed to `lastTimestampAccrued`.

### Likelihood Explanation
Requires `totalSupply_` small enough that per-block interest rounds to zero — realistic for low-decimal synthetics (msBTC, 8 decimals) and low-TVL pools, especially on 2-second-block L2s where deployed instances exist (base, optimism). An unprivileged borrower only needs to send one cheap external call per block; on low-gas L2s the saved interest on a ~$100k debt at 10%+ APR comfortably exceeds gas. No privileged role, oracle manipulation, or timing beyond normal block cadence is needed.

### Recommendation
Only advance `lastTimestampAccrued` when the accrual actually produced a non-zero interest/index update, or track a separate `lastNonZeroAccrualTimestamp` used for the delta computation. Alternatively, accumulate the raw (un-rounded) rate product — e.g., store an accumulated `_interestRateToAccrue` remainder and only advance the timestamp fully, so sub-wad rounding dust is carried forward instead of discarded.

### Proof of Concept
Hardhat-style reproduction against the existing test harness (`test/DebtToken.test.ts`):

```ts
it('blocks interest accrual by calling accrueInterest every block', async function () {
  // setup: low-decimal synth debt token (e.g. msBTC, 8 decimals)
  await msBTCDebt.updateInterestRate(parseEther('0.1')) // 10% APR
  // totalSupply_ kept below rounding threshold, e.g. 0.5 msBTC = 5e7 wei
  await msBTCDebt.connect(user1).issue(5e7, user1.address)

  const debtBefore = await msBTCDebt.balanceOf(user1.address)

  // attacker calls accrueInterest() every block for ~1 year of elapsed time
  for (let i = 0; i < BLOCKS_PER_YEAR; i++) {
    await time.increase(2)               // 2s block cadence (Base/OP)
    await msBTCDebt.accrueInterest()     // permissionless; rounds to 0 but advances lastTimestampAccrued
  }

  const debtAfter = await msBTCDebt.balanceOf(user1.address)
  const supplyAfter = await msBTCDebt.totalSupply()

  // debtIndex and totalSupply never grew despite a year elapsing
  expect(debtAfter).eq(debtBefore)
  expect(supplyAfter).eq(5e7)
  // feeCollector received nothing
  expect(await msBTC.balanceOf(feeCollector.address)).eq(0)
})
```

Contrast: without the per-block calls, `await time.increase(SECONDS_PER_YEAR); await msBTCDebt.accrueInterest()` yields `totalSupply ≈ 1.10 * principal`, matching the existing tests at `test/DebtToken.test.ts:792-801`.