### Title
Stale `debtIndex` written to `debtIndexOf` in `DebtToken._burn` desynchronizes cached per-account index from the virtually-accrued index, inflating residual debt - (contracts/DebtToken.sol)

### Summary
The kernel bug class is a **cached shadow state that diverges from the authoritative state**: `xfd_state` caches `MSR_IA32_XFD`; a reset path updates the real MSR without updating the cache, so a later conditional write is skipped and the system crashes on stale assumptions.

`DebtToken` has the same structure. The authoritative accrual is computed lazily by `_calculateInterestAccrual()` (used by `balanceOf` and `totalSupply`), while `debtIndex` / `lastTimestampAccrued` / `debtIndexOf[account]` are cached copies only refreshed by `accrueInterest()`. `DebtToken._burn` mixes the two: it measures the account's debt with the *fresh virtual* index via `balanceOf(account_)`, but then stores the *stale cached* index into `debtIndexOf[account_]` [1](#0-0) . The external `burn` entry point used by `Pool` does not call `accrueInterest()` itself [2](#0-1) .

### Finding Description
- `balanceOf` returns `principalOf[account] * _debtIndex / debtIndexOf[account]`, where `_debtIndex` is the lazily-computed up-to-date index [3](#0-2) .
- `_burn` sets `principalOf[account_] = balanceOf(account_) - amount_` (fresh index) but `debtIndexOf[account_] = debtIndex` (cached, stale whenever a previous tx accrued interest without calling `accrueInterest`) [4](#0-3) .
- Since `debtIndex` (stored) < `debtIndex` (virtual) whenever time passed since the last accrual, the post-burn residual debt becomes `(oldBalance - amount) * freshIndex / staleIndex > oldBalance - amount`. Every partial burn permanently inflates the victim's remaining debt.
- Paths that call `accrueInterest()` first (`repay`, `repayAll`, `issue`, `mint`, `flashIssue`) keep the cache synchronized [5](#0-4) . The risk concentrates on the `onlyPool` `burn` path used by `Pool.liquidate`: if `Pool` does not call `accrueInterest()` in the same transaction before `debtToken.burn(...)`, every partial liquidation writes a stale `debtIndexOf`. **Note:** I was unable to retrieve `Pool.sol`'s `liquidate` body (the index only returned match counts), so whether that path accrues first is unverified; the latent desync inside `_burn`/`_mint` is confirmed regardless.

The same stale-write pattern exists in `_mint` (`debtIndexOf[account_] = debtIndex` after `balanceOf` uses the virtual index), though all current external mint paths accrue first [6](#0-5) .

### Impact Explanation
Each unsynchronized partial `burn` leaves the victim with more debt than economically burned. Consequences: (a) the inflated phantom debt reduces `unlockedBalanceOf` collateral, potentially freezing funds or pushing healthy positions below liquidation thresholds; (b) `repayAll` computes `_repaid = balanceOf(...)` with the fresh index [7](#0-6) , so the victim must overpay synth to clear debt — direct value extraction; (c) `removeFromDebtTokensOfAccount` may never fire because `balanceOf` never reaches 0, corrupting the account's debt-token list.

### Likelihood Explanation
Requires a `Pool.liquidate` (or any future `onlyPool` caller) that invokes `burn` without a preceding `accrueInterest` in the same transaction, plus a non-zero `interestRate` and elapsed time since the last accrual — both routine conditions. The trigger needs no privileged actor on the DebtToken side; a liquidator is an unprivileged caller.

### Recommendation
Mirror the kernel fix (`xfd_set_state()` writing cache and register together): write `debtIndexOf[account_]` using the *computed* `_debtIndex` rather than the stored `debtIndex`, or call `accrueInterest()` at the top of `burn`/`_burn`/`_mint` instead of relying on callers. Alternatively make `_burn` accept the freshly-computed index so cached and authoritative state can never diverge.

### Proof of Concept
1. Deploy `Pool` + `DebtToken` with `interestRate > 0`; user deposits collateral and issues debt.
2. Advance time so the virtual index exceeds stored `debtIndex`; perform no accruing tx.
3. Execute a `Pool.liquidate` that calls `debtToken.burn(victim, partial)` without prior `accrueInterest` in the same tx.
4. Assert `debtToken.balanceOf(victim) > expectedBalance - partial` and that `debtIndexOf[victim] == staleDebtIndex < debtIndex'`.

Confidence caveat: step 3 depends on `Pool.liquidate`'s accrual ordering, which I could not confirm from the available index; if `Pool` does accrue first, the desync in `_burn` remains a latent bug exploitable by any future non-accruing caller rather than a live one.

### Citations

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

**File:** contracts/DebtToken.sol (L213-215)
```text
    function burn(address from_, uint256 amount_) external override onlyPool {
        _burn(from_, amount_);
    }
```

**File:** contracts/DebtToken.sol (L431-454)
```text
        accrueInterest();

        address _msgSender = _msgSender();
        IPool _pool = pool;
        ISyntheticToken _syntheticToken = syntheticToken;

        (_repaid, _fee) = quoteRepayOut(amount_);
        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, _pool.feeCollector(), _fee);
        }

        uint256 _debtFloorInUsd = _pool.debtFloorInUsd();
        if (_debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = _pool.masterOracle().quoteTokenToUsd(
                address(_syntheticToken),
                balanceOf(onBehalfOf_) - _repaid
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < _debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DebtToken.sol (L478-492)
```text
        _repaid = balanceOf(onBehalfOf_);
        if (_repaid == 0) revert AmountIsZero();

        address _msgSender = _msgSender();
        ISyntheticToken _syntheticToken = syntheticToken;

        uint256 _amount;
        (_amount, _fee) = quoteRepayIn(_repaid);

        if (_fee > 0) {
            _syntheticToken.seize(_msgSender, pool.feeCollector(), _fee);
        }

        _syntheticToken.burn(_msgSender, _repaid);
        _burn(onBehalfOf_, _repaid);
```

**File:** contracts/DebtToken.sol (L525-543)
```text
    function _burn(address account_, uint256 amount_) private updateRewardsBeforeMintOrBurn(account_) {
        if (account_ == address(0)) revert BurnFromNullAddress();

        uint256 _accountBalance = balanceOf(account_);
        if (_accountBalance < amount_) revert BurnAmountExceedsBalance();

        unchecked {
            principalOf[account_] = _accountBalance - amount_;
            debtIndexOf[account_] = debtIndex;
            totalSupply_ -= amount_;
        }

        emit Transfer(account_, address(0), amount_);

        // Remove this token from the debt tokens list if the sender's balance goes to zero
        if (amount_ > 0 && balanceOf(account_) == 0) {
            pool.removeFromDebtTokensOfAccount(account_);
        }
    }
```

**File:** contracts/DebtToken.sol (L581-595)
```text
        uint256 _balanceBefore = balanceOf(account_);

        if (
            _debtFloorInUsd > 0 &&
            masterOracle_.quoteTokenToUsd(address(syntheticToken), _balanceBefore + amount_) < _debtFloorInUsd
        ) {
            revert DebtLowerThanTheFloor();
        }

        totalSupply_ += amount_;
        if (totalSupply_ > maxTotalSupply) revert SurpassMaxDebtSupply();

        principalOf[account_] = _balanceBefore + amount_;
        debtIndexOf[account_] = debtIndex;
        emit Transfer(address(0), account_, amount_);
```
