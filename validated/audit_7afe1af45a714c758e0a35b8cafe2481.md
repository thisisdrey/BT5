### Title
Debt-token rewards accrue on interest-inflated `balanceOf`, letting borrowers claim rewards on interest they did not hold during the period - (File: contracts/RewardsDistributor.sol)

### Summary
`RewardsDistributor._calculateTokenDelta` computes a user's accrued rewards as `token_.balanceOf(account_).wadMul(_deltaIndex)` at accrual time. For `DebtToken`, `balanceOf` is not a static balance — it is `principalOf[account_] * currentDebtIndex / debtIndexOf[account_]`, which grows continuously with accrued interest. Because the reward index delta is only snapshotted when `updateBeforeMintOrBurn` is invoked (and that function is callable by anyone), the entire elapsed reward-index delta is multiplied by the *end-of-period, interest-inflated* debt balance rather than the time-weighted balance. This is the same class as the Derby finding: rewards are computed on an updated balance instead of the balance that actually existed over the accrual period. [1](#0-0) [2](#0-1) 

### Finding Description
- `RewardsDistributor` accrues emissions lazily: `_updateTokenIndex` advances `tokenStates[token_].index` by `speed * elapsed / totalSupply`, and `_updateTokensAccruedOf` credits `accountIndexOf` delta × `token_.balanceOf(account_)`.
- `updateBeforeMintOrBurn` is permissionless ("This function also may be called by anyone to update stored indexes"), so the attacker controls when their account is snapshotted. [3](#0-2) 
- `DebtToken.balanceOf` returns principal scaled by `debtIndex` growth from `_calculateInterestAccrual` — it increases every second without any mint/burn hook firing. [4](#0-3) 
- Therefore a borrower who takes debt at time T0 and lets interest compound until T1 without any balance-changing action gets rewards for the whole [T0, T1] interval computed on `balanceOf(T1)`, which includes all interest accrued during the interval — a balance that never existed for most of the period. In Derby terms: `savedTotalUnderlying` analog — the reward base drifts between snapshot and calculation.
- Additionally, `_calculateTokenIndex` uses `token_.totalSupply()`, which for DebtToken also includes unaccrued pending interest (`totalSupply_ + _interestAmountAccrued`), so the index itself is computed on a supply that does not match stored balances — a second skew in the same direction as the Derby report's "price change multiplied by updated balance" sub-issue. [5](#0-4) 

### Impact Explanation
Direct theft of unclaimed yield from the `RewardsDistributor` reward pool: an attacker captures a larger share of each epoch's emissions than their time-weighted debt position entitles them to, diluting honest DebtToken/DepositToken holders. The extra rewards are paid from real `rewardToken` balance held by the distributor. The exploit needs only public entry points (`DebtToken.issue`, then `RewardsDistributor.claimRewards`), no privileged role, and no price manipulation — interest accrual does the inflation automatically. The effect scales with `interestRate` and elapsed time between reward-index updates; on high-APR debt tokens or infrequently-touched tokens the over-accrual is material.

### Likelihood Explanation
Requires only that (a) a rewards distributor is registered for a `DebtToken` with `tokenSpeeds > 0`, and (b) `interestRate > 0` on that debt token — both normal deployed configurations. The attacker just issues debt and claims later; no timing race, no flash loan needed (though a flash-funded large borrow amplifies it). Every second of delay between `updateBeforeMintOrBurn` calls inflates the attacker's reward base by the interest accrued in that window.

### Recommendation
Decouple reward accounting from `DebtToken.balanceOf`'s interest growth: either accrue rewards against `principalOf[account_]` (the stored principal, not the index-grown view), or exclude DebtTokens from reward distribution, or force `accrueInterest()` + `updateBeforeMintOrBurn` for the account atomically so the index delta never spans an interest-growth window. For `_calculateTokenIndex`, use a non-interest-bearing supply measure for DebtTokens.

### Proof of Concept
Hardhat fork/unit test outline:

```ts
// Setup: pool with debtToken (interestRate = 10% APR) registered in rewardsDistributor, speed = 1 reward/s
// 1. Attacker calls debtToken.issue(amount, attacker) → principalOf[attacker] = X, debtIndexOf = idx0
//    updateBeforeMintOrBurn fires → accountIndexOf[debtToken][attacker] = currentIndex
// 2. Advance time T (no tx touching attacker). Interest accrues only virtually.
// 3. Attacker calls rewardsDistributor.claimRewards(attacker, [debtToken])
//    → _updateTokenIndex: indexDelta = speed*T / totalSupply()
//    → _updateTokensAccruedOf: delta = debtToken.balanceOf(attacker) * indexDelta
//      where balanceOf(attacker) = X * debtIndex(T) / idx0  ≈ X * (1 + r*T)
// Expected (fair): delta ≈ X * indexDelta (time-weighted, interest grows linearly so ~X*(1+r*T/2))
// Actual: delta uses full X*(1+r*T) — attacker is overpaid by ~X*r*T/2*indexDelta
// Assert: tokensAccruedOf[attacker] > X * indexDelta
```

The assertion gap grows quadratically in `T` and linearly in `interestRate`, confirming reward theft proportional to unheld interest.

### Citations

**File:** contracts/RewardsDistributor.sol (L175-180)
```text
    function updateBeforeMintOrBurn(IERC20 token_, address account_) external override {
        if (tokenStates[token_].index > 0) {
            _updateTokenIndex(token_);
            _updateTokensAccruedOf(token_, account_);
        }
    }
```

**File:** contracts/RewardsDistributor.sol (L229-230)
```text
        uint256 _deltaIndex = _tokenIndex - _accountIndex;
        _tokensDelta = token_.balanceOf(account_).wadMul(_deltaIndex);
```

**File:** contracts/DebtToken.sol (L196-205)
```text
    function balanceOf(address account_) public view override returns (uint256) {
        uint256 _principal = principalOf[account_];
        if (_principal == 0) {
            return 0;
        }

        (, uint256 _debtIndex, ) = _calculateInterestAccrual();

        // Note: The `debtIndex / debtIndexOf` gives the interest to apply to the principal amount
        return (_principal * _debtIndex) / debtIndexOf[account_];
```

**File:** contracts/DebtToken.sol (L500-503)
```text
    function totalSupply() external view override returns (uint256) {
        (uint256 _interestAmountAccrued, , ) = _calculateInterestAccrual();
        return totalSupply_ + _interestAmountAccrued;
    }
```

**File:** contracts/DebtToken.sol (L551-565)
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
```
