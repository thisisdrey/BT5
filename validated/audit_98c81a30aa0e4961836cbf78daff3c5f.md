### Title
`DebtToken.burn()` skips `accrueInterest()`, corrupting `debtIndexOf` with a stale index on the liquidation path - ([File: contracts/DebtToken.sol](contracts/DebtToken.sol))

### Summary
Every state-changing entry point in `DebtToken` (`issue`, `mint`, `flashIssue`, `repay`, `repayAll`, `updateInterestRate`) calls `accrueInterest()` before touching debt state — except `burn()`, which is the function `Pool.liquidate` uses to write off a liquidated account's debt. `_burn()` computes the account balance with the *virtually accrued* index but then stores `debtIndexOf[account_] = debtIndex` using the *stale, un-accrued* stored index. This is the direct analog of `cpuset_attach()` being reachable from `cgroup_attach_task_all()` without the required `cpus_read_lock()`: a callee that assumes a prerequisite (fresh `debtIndex`) is invoked from a caller path that never establishes it.

### Finding Description
In `contracts/DebtToken.sol`:

- `issue()` (line 248), `flashIssue()` (line 296), `mint()` (line 340), `repay()` (line 431), `repayAll()` (line 476) all call `accrueInterest()` before mutating `principalOf`/`debtIndexOf`.
- `burn(address from_, uint256 amount_)` at line 213 is `onlyPool` and calls `_burn` directly with **no** `accrueInterest()`.
- Inside `_burn` (lines 525–543): `_accountBalance = balanceOf(account_)` uses `_calculateInterestAccrual()`, which returns the *projected* `_debtIndex` (line 202–205), but then `debtIndexOf[account_] = debtIndex` writes the **stale stored** `debtIndex` (line 533), since `accrueInterest()` was never run to persist it.

So on a partial burn, the remaining `principalOf[account_]` is a post-burn value computed at accrued index `I_new`, but the recorded denominator `debtIndexOf` is the old `I_old < I_new`. All future `balanceOf` reads compute `principal * futureIndex / I_old`, re-applying interest that was already embedded in the principal — the victim's remaining debt grows faster than the global index. `totalSupply_` is likewise decremented against accrued balance while the stored `debtIndex` is never advanced. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
An unprivileged liquidator calling `Pool.liquidate` reaches `DebtToken.burn` without any prior `accrueInterest()` (unless the pool happens to call it — no `accrueInterest` call exists in `DebtToken.burn` itself, and `balanceOf`/`totalSupply` only compute the accrual virtually). The stale `debtIndexOf` write permanently inflates the liquidated user's residual debt and desynchronizes per-account index bookkeeping from `debtIndex`, breaking the debt-conservation invariant (`sum(principal * index/indexOf) == totalSupply`). Consequences: (a) residual debt that grows faster than the real interest rate, pushing victims into wrongful subsequent liquidations (direct extraction of their collateral by any liquidator); (b) accounting drift between `totalSupply_` and per-user principal that can produce bad-debt mismeasurement and erroneous `maxLiquidable`/`debtFloorInUsd` decisions — protocol solvency/accounting integrity impact.

### Likelihood Explanation
Trigger requires only an active `DebtToken` with nonzero `interestRate` and any liquidation performed after interest accrual time has elapsed — the common case on a live deployment. No privileged role is needed: the liquidator path is open to any EOA/contract. Severity of the distortion scales with time since `lastTimestampAccrued` and the interest rate. Note: confirmation that `Pool.liquidate` does not itself call `accrueInterest()` before `debtToken.burn` should be verified on the deployed `Pool` bytecode (the search index did not expose `Pool.sol` bodies to me); if `Pool` does accrue first, the window collapses and this downgrades to a latent inconsistency.

### Recommendation
Call `accrueInterest()` at the top of `burn()` (or inside `_burn`), matching every other mutator, so `debtIndexOf[account_] = debtIndex` always records the fresh index:

```solidity
function burn(address from_, uint256 amount_) external override onlyPool {
    accrueInterest();
    _burn(from_, amount_);
}
```

Alternatively, have `_burn` persist `debtIndex = _calculateInterestAccrual()` result before writing `debtIndexOf`.

### Proof of Concept
Foundry fork/unit sketch:

```solidity
// Setup: pool, depositToken (collateralFactor e.g. 0.8e18), debtToken (interestRate > 0),
// syntheticToken msX. Victim deposits collateral and issues msX debt.
vm.warp(block.timestamp + 30 days); // interest accrues virtually

// Attacker manipulates/donates price or waits until victim is liquidatable, then:
pool.liquidate(syntheticToken, victim, depositToken, amountIn, attacker);
// internally -> debtToken.burn(victim, repaid) with NO accrueInterest()

// After burn, force accrual and read:
debtToken.accrueInterest();
uint256 recorded = debtToken.balanceOf(victim);
// Expected: principal * debtIndex / debtIndexOf where debtIndexOf == post-accrual index at burn time.
// Actual: debtIndexOf[victim] == stale pre-accrual debtIndex, so `recorded` is inflated by
// roughly (1 + rate*30d). Assert recorded > expectedHonestResidual.
```

The delta `debtIndex_new / debtIndex_stale - 1` quantifies the phantom debt re-applied to the victim's residual principal on every subsequent read.

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

**File:** contracts/DebtToken.sol (L213-215)
```text
    function burn(address from_, uint256 amount_) external override onlyPool {
        _burn(from_, amount_);
    }
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
