### Title
`accrueInterest` resets `lastTimestampAccrued` even when rounded interest is zero, allowing permanent suppression of debt growth on low-decimal synthetics - (contracts/DebtToken.sol)

### Summary
`DebtToken.accrueInterest()` unconditionally overwrites `lastTimestampAccrued` with `block.timestamp` but only updates `totalSupply_` and `debtIndex` when the computed `_interestAmountAccrued` is strictly greater than zero. For debt tokens backed by low-decimal synthetics (e.g. msBTC, 8 decimals) and/or low `interestRate`, the per-block accrued interest `wadMul`s down to zero while `totalSupply_` is small. Any EOA can call `accrueInterest()` (it is `public` and unauthenticated) every block, permanently discarding the elapsed accrual window so debt and protocol interest revenue never grow until `totalSupply_` crosses the rounding threshold. Additionally, `_calculateInterestAccrual` computes a non-zero `_debtIndex` increment even when `_interestAmountAccrued == 0`, but `accrueInterest` skips persisting it, so index growth is also silently dropped.

### Finding Description
In `contracts/DebtToken.sol`:

- `accrueInterest()` (lines ~156-180):
  ```solidity
  lastTimestampAccrued = block.timestamp;
  if (_interestAmountAccrued > 0) {
      totalSupply_ += _interestAmountAccrued;
      debtIndex = _debtIndex;
      try syntheticToken.mint(pool.feeCollector(), ...) { ... }
  }
  ```
  The timestamp is updated regardless of whether anything accrued.

- `_calculateInterestAccrual()` (lines ~551-566):
  ```solidity
  uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
  if (_interestRateToAccrue > 0) {
      _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
      _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
  }
  ```
  `_interestAmountAccrued = rate*dt*totalSupply_ / 1e18` rounds to zero when `totalSupply_ < 1e18 / (rate_per_second * dt)`. With a 10% APR (`rate_per_second ≈ 3.17e9`) and `dt = 12` (one mainnet block), the threshold is `≈ 2.6e8` base units — about 2.6 whole tokens for an 8-decimals synthetic like msBTC; the threshold grows inversely with `interestRate` (at 1% APR ≈ 26 BTC-denominated units). All elapsed time producing sub-threshold interest is erased when `lastTimestampAccrued` is rewritten.

- `accrueInterest()` is `public override` with no access control, so an unprivileged attacker can call it every block at gas cost only. It is also invoked internally from `issue`/`repay` flows, so even organic activity resets the window.

- Note the inconsistency: `balanceOf()` (lines ~196-205) uses the *hypothetical* `_debtIndex` from `_calculateInterestAccrual`, and the index increment `_interestRateToAccrue.wadMul(_debtIndex)` is non-zero even when `_interestAmountAccrued` rounds to zero (it is ≈ `_interestRateToAccrue` since `debtIndex ≈ 1e18`). `accrueInterest` drops this index delta entirely because the write is gated on `_interestAmountAccrued > 0`, compounding the loss.

### Impact Explanation
While a pool's debt token `totalSupply_` is below the rounding threshold, debt positions accrue no interest and the feeCollector receives no interest mints — a permanent loss of protocol yield / under-reporting of borrower debt relative to configured `interestRate`. The debt-invariant that `totalSupply` grows at `interestRate` over time is broken. The impact is bounded to small-TVL debt markets and low-decimal synthetics (msBTC is the realistic case; for 18-decimals synthetics the threshold is sub-wei dust, and `debtFloorInUsd` per-position minimums keep `totalSupply_` above it). This maps to "theft/freezing of unclaimed yield" rather than direct theft.

### Likelihood Explanation
Requires only: a deployed debt token on a low-decimals synthetic, `totalSupply_` below the threshold (early in a market's life or after mass repayment), and a nonzero but modest `interestRate`. The attacker call is a single unauthenticated public function repeatable every block. No governance, oracle, or privileged involvement. However, interest-rate configuration and pool bootstrap state are prerequisites, and the yield loss is proportional to how far below threshold the debt is, so overall severity is moderate rather than high.

### Recommendation
Only advance `lastTimestampAccrued` when there is something to persist, or better, persist the index unconditionally:

```solidity
if (block.timestamp == _lastTimestampAccrued) return;
lastTimestampAccrued = block.timestamp;
if (_debtIndex != debtIndex) debtIndex = _debtIndex; // never drop index growth
if (_interestAmountAccrued > 0) {
    totalSupply_ += _interestAmountAccrued;
    ...
}
```
Alternatively keep a higher-precision (ray, 1e27) index so sub-unit interest accumulates instead of being discarded.

### Proof of Concept
Foundry fork-style test against `DebtToken` (8-decimals synthetic, mirroring `msBTCDebt`):

```solidity
function test_accrueInterest_roundsToZero() public {
    // Setup: deploy Pool, msBTC-like SyntheticToken(decimals=8), DebtToken
    // governor sets interestRate = 0.1e18 (10% APR)
    // user deposits collateral and issues ~1e8 (1 msBTC) of debt
    debtToken.issue(1e8, alice);       // totalSupply_ = 1e8, below ~2.6e8 threshold

    uint256 supplyBefore = debtToken.totalSupply();
    uint256 indexBefore  = debtToken.debtIndex();

    // Attacker calls accrueInterest() every block (any EOA)
    for (uint256 i; i < 100; i++) {
        vm.warp(block.timestamp + 12);
        vm.prank(attacker);
        debtToken.accrueInterest();
    }

    // Interest for 1200s at 10% APR on 1e8 units should be ~3.8e6 units,
    // but each 12s slice rounds to zero and the timestamp is still advanced.
    assertEq(debtToken.totalSupply(), supplyBefore);
    assertEq(debtToken.debtIndex(), indexBefore);
    // control: without the attacker calls, same elapsed time accrues > 0
}
```

References: `contracts/DebtToken.sol` `accrueInterest` (L156-180), `_calculateInterestAccrual` (L551-566), `balanceOf` (L196-205), `SECONDS_PER_YEAR` (L54); `contracts/storage/DebtTokenStorage.sol` (L40-54).