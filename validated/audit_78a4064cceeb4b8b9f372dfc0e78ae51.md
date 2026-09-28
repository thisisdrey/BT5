### Title
`DebtToken._burn` checkpoints `debtIndexOf` against the stale stored `debtIndex`, double-counting accrued interest when `burn` is invoked without prior `accrueInterest()` — (`contracts/DebtToken.sol`)

### Summary
`DebtToken._burn` computes the account's live debt with `balanceOf()` — which internally applies the *pending* (not yet stored) `debtIndex` via `_calculateInterestAccrual()` — but then persists the checkpoint `debtIndexOf[account_] = debtIndex` using the **stored** index. All debt-mutating entry points inside `DebtToken` (`issue`, `repay`, `repayAll`, `mint`, `flashIssue`) call `accrueInterest()` first, but `burn()` (callable by `Pool`, e.g. from `Pool.liquidate`) does not accrue. If a burn lands before accrual, the interest-inclusive remainder is re-based on the stale index and interest is charged twice.

### Finding Description [1](#0-0)  `balanceOf` returns `principal * pendingDebtIndex / debtIndexOf[account]`, where `pendingDebtIndex` already includes unaccrued interest. [2](#0-1)  In `_burn`:
- `principalOf[account_] = balanceOf(account_) - amount_` → principal now already contains accrued interest.
- `debtIndexOf[account_] = debtIndex` → stored (stale, lower) index.
- Next `balanceOf` = `principal * newIndex / staleIndex` → the elapsed interest is applied a second time.

Contrast with `issue`/`repay`/`repayAll`, which call `accrueInterest()` before mutating state (`contracts/DebtToken.sol:248`, `:431`, `:476`), making stored `debtIndex == pendingDebtIndex` at checkpoint time. `burn(address,uint256)` at `contracts/DebtToken.sol:213` performs no accrual; it relies on the `Pool` caller having accrued. The pending-fee path (`pendingInterestFee`, `getPendingInterestFee`) shows the designers know accrued state can lag storage.

### Impact Explanation
Any user whose debt is partially burned through a path that does not accrue interest first (the `onlyPool` `burn` entry point, reached during liquidation) has their remaining debt permanently inflated by the unaccrued interest factor. Consequences: the victim's position is pushed further underwater (wrongful/extra liquidations follow), the victim must repay synthetic tokens that were never minted to them, and aggregate `balanceOf` sums diverge from `totalSupply_`/`SyntheticToken` supply — a solvency/accounting invariant break analogous to the reported `exchangeRateStored` miscrediting.

### Likelihood Explanation
Requires only that `block.timestamp > lastTimestampAccrued` when a burn occurs — i.e., effectively always, since `accrueInterest()` is permissionless but not forced inside `burn`. An unprivileged liquidator calling `Pool.liquidate` triggers the stale-index checkpoint whenever the pool does not accrue the debt token first (no accrual call exists inside `DebtToken.burn` itself). Frequency scales with liquidation activity; magnitude scales with elapsed time × `interestRate`.

### Recommendation
- Call `accrueInterest()` inside `burn()` (or at the top of `_burn`/`_mint`) so checkpointing always uses the fresh index — mirror the fix `exchangeRateStored → exchangeRateCurrent`.
- Alternatively store the freshly computed index: `debtIndexOf[account_] = pendingIndex` returned by `_calculateInterestAccrual()`.
- Ensure `Pool.liquidate` (and any other `burn` caller) accrues the debt token before burning.

### Proof of Concept
Hardhat sketch (contracts test env, per `test/DebtToken.test.ts` patterns):

```ts
// setup: user1 deposits collateral and issues 100 msUSD debt
await msUSDDebt.updateInterestRate(parseEther('0.1')) // 10% APR
await time.increase(SECONDS_PER_YEAR)               // interest accrues in views only

// Simulate a partial burn WITHOUT accruing (the path burn() takes via Pool.liquidate
// when the pool does not call accrueInterest first):
// Directly exercise it by calling burn through the pool-authorized account,
// or via pool.liquidate(...) for a dust amount (e.g. repay 1 wei of debt).

const debtBefore = await msUSDDebt.balanceOf(user1.address) // ~110 (includes pending interest)
await pool.liquidate(msUSD.address, user1.address, dustRepayAmount, depositToken.address)

// After burn: principalOf = ~110 - dust, but debtIndexOf = old stored index (1e18-side)
// Expected: balanceOf(user1) ~= 110 - dust
// Actual:   balanceOf(user1) ~= (110 - dust) * 1.1  → ~121 - dust
// i.e. ~10 phantom msUSD of debt is manufactured; next accrual inflates it further.

const debtAfter = await msUSDDebt.balanceOf(user1.address)
expect(debtAfter).to.be.gt(debtBefore.sub(dustRepayAmount)) // fails — debt grew instead of shrinking
```

Verification point for the PoC: confirm whether `Pool.liquidate` calls `debtToken.accrueInterest()` before `debtToken.burn` — I could not inspect `Pool.sol`'s liquidate body in this pass (grep returned no line-level output). If the pool does accrue first, the bug is latent but still triggerable through any future/direct `onlyPool` burn path; if it does not, the dust-liquidation path above reproduces it directly. The core defect — `_burn` writing the stale stored `debtIndex` while `principalOf` was computed at the pending index — is confirmed in `contracts/DebtToken.sol:525-543`.

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
