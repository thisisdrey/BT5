### Title
Anyone can inflate borrowers' debt above the configured APR by forcing near-continuous compounding via permissionless `accrueInterest()` - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
`DebtToken.accrueInterest()` is `public` and unguarded. Each call folds the linearly-computed interest into `totalSupply_` and `debtIndex`, so the next accrual is computed on the already-grown base. The effective compounding frequency is therefore controlled by how often any EOA calls `accrueInterest()` (or any pool operation that triggers it), letting an attacker push borrowers' realized APR toward the continuous-compounding bound (~5% above the nominal rate at 10% APR).

### Finding Description
`_calculateInterestAccrual()` computes simple interest `rate_per_second * dt * totalSupply_` on the *current stored* supply: [1](#0-0) 

`accrueInterest()` then persists the accrued amount into `totalSupply_` and bumps `debtIndex`, so interest starts accruing on interest — classic index compounding where frequency = call frequency: [2](#0-1) 

Unlike Surge, no dust deposit or pool interaction is needed: `accrueInterest()` has no modifier at all. An attacker calls it every block (or every few seconds) for a year at gas cost only. `issue`/`flashIssue` also call it ( [3](#0-2) ), so even absent an attacker, realized borrower APR varies with protocol activity — the same "variable compounding frequency" defect, trivially reachable by an unprivileged account. Borrower debt is denominated via `balanceOf` = `principalOf * debtIndex / debtIndexOf` ( [4](#0-3) ), which tracks the inflated index.

### Impact Explanation
All borrowers pay strictly more than the configured `interestRate` — at 10% nominal APR, continuous compounding yields ~10.517% effective, a ~5% excess on the entire outstanding debt. The excess is minted as synthetic tokens to `pool.feeCollector()` ( [5](#0-4) ), i.e., user funds are extracted beyond the protocol's stated rate. Additionally, the inflated `debtIndex` raises every position's `debtOf`, which can push borderline positions under the collateral-ratio floor and make them liquidatable when they otherwise would not be — a liquidator can run `accrueInterest()` each block to accelerate that and profit from the liquidation discount.

### Likelihood Explanation
High feasibility: `accrueInterest()` is callable by any EOA with zero collateral, no deposit, no health check, no `whenNotShutdown`/guard restrictions. Cost is only gas per call; it needs just a few calls per period to approach the continuous-compounding limit (compounding converges quickly — daily compounding already captures ~99.6% of the continuous excess). No privileged role, oracle manipulation, or timing dependency is required. The magnitude is bounded (~5% max over nominal APR), consistent with medium severity.

### Recommendation
Either:
- Accrue interest on a fixed principal base, not on `totalSupply_` that already embeds accrued interest (analogous to the Surge recommendation: track `totalPrincipal` and compute interest against it), or
- Embrace explicit compounding and fix the frequency: compute the index with `debtIndex * (1 + ratePerSecond)^dt` (or `exp(rate*dt)`), which is call-frequency-independent and gives a deterministic effective rate.

The second option is the cleaner fix for an index-based design, since it makes the outcome identical whether `accrueInterest()` is called once a year or once a second.

### Proof of Concept
Hardhat sketch (pattern matches existing tests in `test/DebtToken.test.ts`):

```ts
it('attacker forces near-continuous compounding', async function () {
  await msdMET.connect(user1).deposit(parseEther('1000'), user1.address)
  await msUSDDebt.connect(user1).issue(parseEther('100'), user1.address)
  await msUSDDebt.updateInterestRate(parseEther('0.10')) // 10% APR

  // Baseline: no accrual during the year -> simple interest ~110
  // Attack path: call accrueInterest() every ~1 day for a year
  const days = 365
  for (let i = 0; i < days; i++) {
    await time.increase(time.duration.days(1))
    await msUSDDebt.accrueInterest() // permissionless, any EOA
  }

  const debt = await msUSDDebt.balanceOf(user1.address)
  // debt ≈ 100 * e^0.10 ≈ 110.517 vs ~110.00 with single yearly accrual
  expect(debt).to.be.gt(parseEther('110.4'))
})
```

A fork-level variant: interleave `accrueInterest()` calls with `pool.debtPositionOf(victim)` reads to show a borderline victim's `_isHealthy` flipping to `false` earlier than under simple accrual, then execute `pool.liquidate` for profit.

### Citations

**File:** contracts/DebtToken.sol (L156-180)
```text
    function accrueInterest() public override {
        (
            uint256 _interestAmountAccrued,
            uint256 _debtIndex,
            uint256 _lastTimestampAccrued
        ) = _calculateInterestAccrual();

        if (block.timestamp == _lastTimestampAccrued) {
            return;
        }

        lastTimestampAccrued = block.timestamp;

        if (_interestAmountAccrued > 0) {
            totalSupply_ += _interestAmountAccrued;
            debtIndex = _debtIndex;

            // Note: Address states where minting will fail (e.g. the token is inactive, it reached max supply, etc)
            try syntheticToken.mint(pool.feeCollector(), _interestAmountAccrued + pendingInterestFee) {
                pendingInterestFee = 0;
            } catch {
                pendingInterestFee += _interestAmountAccrued;
            }
        }
    }
```

**File:** contracts/DebtToken.sol (L196-206)
```text
    function balanceOf(address account_) public view override returns (uint256) {
        uint256 _principal = principalOf[account_];
        if (_principal == 0) {
            return 0;
        }

        (, uint256 _debtIndex, ) = _calculateInterestAccrual();

        // Note: The `debtIndex / debtIndexOf` gives the interest to apply to the principal amount
        return (_principal * _debtIndex) / debtIndexOf[account_];
    }
```

**File:** contracts/DebtToken.sol (L248-248)
```text
        accrueInterest();
```

**File:** contracts/DebtToken.sol (L551-566)
```text
    function _calculateInterestAccrual()
        private
        view
        returns (uint256 _interestAmountAccrued, uint256 _debtIndex, uint256 _lastTimestampAccrued)
    {
        _lastTimestampAccrued = lastTimestampAccrued;
        _debtIndex = debtIndex;

        if (block.timestamp > _lastTimestampAccrued) {
            uint256 _interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
            if (_interestRateToAccrue > 0) {
                _interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
                _debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
            }
        }
    }
```
