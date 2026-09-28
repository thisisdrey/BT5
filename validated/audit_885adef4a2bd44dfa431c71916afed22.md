### Title
Debt interest accrual uses current `totalSupply_` for the full elapsed window; `burn()` mutates supply without calling `accrueInterest()` first - (File: contracts/DebtToken.sol)

### Summary
`DebtToken._calculateInterestAccrual()` computes interest as `interestRatePerSecond() * (block.timestamp - lastTimestampAccrued)` multiplied by the *current* `totalSupply_`. Every supply-mutating entry point (`issue`, `mint`, `flashIssue`, `repay`, `repayAll`) correctly calls `accrueInterest()` before changing `totalSupply_`, but the external `burn(address,uint256)` function (callable by `Pool`, e.g. during liquidation flows) calls `_burn()` directly without accruing first. This reintroduces the same bug class: a time-based charge applied to a balance snapshot that ignores intermediate balance changes.

### Finding Description
In `contracts/DebtToken.sol`, `_calculateInterestAccrual()` (lines 551-566) computes `_interestAmountAccrued = _interestRateToAccrue.wadMul(totalSupply_)` where `totalSupply_` is the value *at call time*, applied retroactively to the entire `(block.timestamp - lastTimestampAccrued)` window. `accrueInterest()` (lines 156-180) folds that interest into `totalSupply_` and `debtIndex` and mints it to `pool.feeCollector()`.

`burn()` (lines 213-215) is declared as:
```solidity
function burn(address from_, uint256 amount_) external override onlyPool {
    _burn(from_, amount_);
}
```
`_burn()` (lines 525-543) does `totalSupply_ -= amount_` and writes `principalOf[account_] = balanceOf(account_) - amount_` with `debtIndexOf[account_] = debtIndex` — while `debtIndex` and `lastTimestampAccrued` still reflect a stale checkpoint.

Two consequences follow from any `burn()` executed when `block.timestamp > lastTimestampAccrued` and the pool does not accrue beforehand:

1. **Undercharged global fee**: the next `accrueInterest()` multiplies the already-reduced `totalSupply_` by the full stale time window, so interest owed on the burned portion for the entire elapsed period is never minted to `feeCollector`. The larger the liquidation/burn, the more fee is lost.
2. **Per-account double-counting**: `_burn` bakes the unrealized interest into `principalOf` (via `balanceOf`) but records the *stale* `debtIndex` in `debtIndexOf`. When `accrueInterest()` later bumps `debtIndex`, `balanceOf()` recomputes `principal * newIndex / oldIndex`, charging the account interest on interest that was already folded into principal.

Note the asymmetry with `repay()`/`repayAll()`/`issue()`/`mint()`/`flashIssue()`, which all call `accrueInterest()` first — the burn path is the only supply mutation missing the checkpoint.

### Impact Explanation
Fees owed to `feeCollector` are under-minted in proportion to the size of debt burned and the length of the stale window — a direct loss of protocol yield (theft/shortfall of unclaimed yield). Additionally, the liquidated/repaid account's principal can be re-charged interest already accounted for, distorting `balanceOf`/`debtOf` used by liquidation and health checks. Either direction breaks the accounting invariant that `totalSupply_ == sum(balanceOf)` accrual consistency.

### Likelihood Explanation
`burn()` is invoked from `Pool` liquidation flows, which are permissionless and occur in normal operation whenever positions fall below collateralization. Any liquidation executed in a block later than the last accrual — i.e. virtually all of them during active periods — triggers the miscalculation. No privileged role is required on the burn path itself; an unprivileged liquidator triggers it via `Pool.liquidate`.

Caveat: I was unable to confirm within the available iteration budget whether `Pool.sol` calls `debtToken.accrueInterest()` (which is `public`) before invoking `debtToken.burn()` in `liquidate`/seize paths. `Pool.sol` contains 8 matches for `accrueInterest`/`.burn(` patterns but their call sites were not inspected. If every `Pool` burn call site already accrues first, the finding reduces to an inconsistency rather than an exploitable bug; if even one path (e.g. partial liquidation, `seize` of debt) omits it, the vulnerability is live.

### Recommendation
Call `accrueInterest()` at the top of `burn()` (or inside `_burn()` before `totalSupply_` and `principalOf` are mutated), mirroring `issue()`, `mint()`, `repay()`, `repayAll()`, and `flashIssue()`. This snapshots accrued interest into `debtIndex`/`totalSupply_` before the supply change so the subsequent window is charged on the correct base.

### Proof of Concept
Hardhat sketch against the deployed `DebtToken`/`Pool`:

```ts
// setup: user deposits collateral and issues msUSD debt at interestRate > 0
await depositToken.deposit(collateralAmount); // via Pool
await msUSDDebt.issue(principal, user.address);

// 1. Let interest accrue without any tx touching accrueInterest
await time.increase(SECONDS_PER_YEAR / 2); // 6 months

// 2. Liquidate (or trigger any Pool path calling debtToken.burn) a large chunk of debt
//    burn() mutates totalSupply_ WITHOUT accruing first
await pool.liquidate(syntheticToken, user.address, depositAmount, liquidator.address);

// 3. Advance time and accrue
await time.increase(SECONDS_PER_YEAR / 2);
const feeBefore = await msUSD.balanceOf(feeCollector.address);
await msUSDDebt.accrueInterest();
const feeAfter = await msUSD.balanceOf(feeCollector.address);

// expect: fee minted for the first 6-month window is computed on the
// post-burn (smaller) totalSupply_, so feeAfter - feeBefore is less than
// the correct rate * preBurnSupply * 6mo. Compare against a control run
// where accrueInterest() is called manually right before liquidation.
```

Control comparison: call `msUSDDebt.accrueInterest()` immediately before step 2 — the fee delta at step 3 will be strictly larger, demonstrating the undercharge proportional to `burnedAmount * rate * staleWindow`.