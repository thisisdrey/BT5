### Title
Frequency-dependent discrete compounding in `DebtToken.accrueInterest` makes effective borrow APR diverge from `interestRate` — (File: contracts/DebtToken.sol)

### Summary
`DebtToken._calculateInterestAccrual()` applies the linear rate `interestRatePerSecond() * dt` multiplicatively to the globally accumulated `debtIndex`/`totalSupply_` only at the discrete moments `accrueInterest()` executes (DebtToken.sol:551-566, 156-180). Since `accrueInterest()` is `public` and permissionless, any EOA can dictate the compounding frequency by calling it (or by triggering it via `issue`, `repay`, `mint`, etc., which all call `accrueInterest()` first — DebtToken.sol:340, 431). This is the same bug class as the Flayer `calculateCompoundedFactor` finding: a discrete compounding formula whose result depends on the frequency of unrelated calls, not on the borrower's own position lifetime.

### Finding Description
Per accrual step the index is updated as:

```solidity
_interestRateToAccrue = interestRatePerSecond() * (block.timestamp - _lastTimestampAccrued);
_interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_);
_debtIndex += _interestRateToAccrue.wadMul(_debtIndex);
```

Because `_debtIndex` and `totalSupply_` already contain previously accrued interest, each call effectively multiplies the index by `(1 + r·dt)`. If accruals happen `n` times over a year, the realized factor is `(1 + r/n)^n`, which ranges from `1 + r` (single accrual) up to `e^r` (per-block accrual):

- If `interestRate` is intended as a simple APR, an attacker calling `accrueInterest()` every block pushes borrowers' debt toward `e^r − 1` — e.g. at 100% APR borrowers pay ~171% instead of 100% (~71% more).
- If `interestRate` is intended as a continuously compounded rate, infrequent accrual (low-activity pools) makes the protocol under-collect — e.g. at 100% APR only 100% is collected vs ~171% (~42% less).

`balanceOf`/`debtPositionOf` correctly use the same index so accounting is internally consistent; the invariant that breaks is "debt grows at the configured `interestRate` regardless of third-party call frequency."

### Impact Explanation
Either borrowers are overcharged interest (loss of user funds via inflated debt, minted as synthetic tokens to `feeCollector` at DebtToken.sol:174) or the protocol under-collects interest (insolvency-side loss). The magnitude is bounded by `e^r − 1 − r`, but grows superlinearly with the configured APR and is fully attacker-influenceable since `accrueInterest()` is unauthenticated.

### Likelihood Explanation
High. No privileged role is needed — `accrueInterest()` is public, and every `issue`/`repay`/`mint`/`liquidate`-adjacent debt operation also triggers it. Active pools on Base/Optimism will naturally accrue nearly every block, pushing effective APR toward `e^r` regardless of governance intent.

### Recommendation
Decouple index growth from call frequency: use per-second compounding (`debtIndex *= rpow(1 + interestRatePerSecond, dt)`) or continuous compounding (`debtIndex * exp(r·dt)`), computed lazily in `_calculateInterestAccrual` so `accrueInterest()` only checkpoints a deterministic value. Alternatively, document `interestRate` as the simple-APR-per-accrual and accept the drift, capping `interestRate` so `e^r − 1 − r` stays negligible.

### Proof of Concept
Foundry test against `DebtToken` (drop into `test/foundry`):

```solidity
function test_FrequencyDependentCompounding() public {
    // debt token with 100% APR (interestRate = 1e18), some issued debt exists
    debtToken.updateInterestRate(1 ether);

    // Path A: single accrual over a year
    uint256 idxA = debtToken.debtIndex();
    vm.warp(block.timestamp + 365 days);
    debtToken.accrueInterest();
    uint256 factorA = debtToken.debtIndex() * 1e18 / idxA; // == 2e18

    // reset (fresh fork), Path B: per-day accruals
    uint256 idxB = debtToken.debtIndex();
    for (uint256 i; i < 365; ++i) {
        vm.warp(block.timestamp + 1 days);
        debtToken.accrueInterest(); // permissionless - any EOA
    }
    uint256 factorB = debtToken.debtIndex() * 1e18 / idxB; // ~= e*1e18

    // Same configured APR, ~36% different realized factor — controlled by an unprivileged caller
    assertGt(factorB, factorA * 13 / 10);
}
```

On `main` code, `factorA ≈ 2e18` while `factorB ≈ 2.714e18` for `interestRate = 1e18`, and borrower `balanceOf` scales linearly with the index, demonstrating attacker-controlled over/under-collection with no privileged role required. [1](#0-0) [2](#0-1) [3](#0-2)

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

**File:** contracts/DebtToken.sol (L319-321)
```text
    function interestRatePerSecond() public view override returns (uint256) {
        return interestRate / SECONDS_PER_YEAR;
    }
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
