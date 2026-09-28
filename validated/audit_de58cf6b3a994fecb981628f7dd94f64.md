### Title
`Pool.liquidate` dead zone: positions with debt in `(debtFloorInUsd, debtFloorInUsd / (1 - maxLiquidable))` can never be liquidated, causing protocol bad debt - ([File: contracts/Pool.sol](contracts/Pool.sol))

### Summary
CVE-2018-7713 is a denial of service caused by a bounds assertion (`size.width <= (1<<20)`) that unconditionally aborts processing for a reachable class of inputs. The Metronome analog is the pair of bounds checks in `Pool.liquidate`: `AmountGreaterThanMaxLiquidable` (caps repayment at `maxLiquidable` = 50% of the debt balance) and `RemainingDebtIsLowerThanTheFloor` (reverts if post-liquidation debt is in `(0, debtFloorInUsd)`). For a whole band of debt sizes both checks cannot be satisfied simultaneously, so `liquidate` reverts on every input — a permanent assertion-style DoS on the liquidation path that strands unhealthy positions as protocol bad debt.

### Finding Description
`liquidate` enforces two invariants on `amountToRepay_`:

- `amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable` reverts, so a caller can repay at most `maxLiquidable` (default `0.5e18`, i.e. 50%) of the account's debt-token balance [1](#0-0) 
- If `debtFloorInUsd > 0`, any repayment that leaves a remaining debt strictly between 0 and `debtFloorInUsd` reverts with `RemainingDebtIsLowerThanTheFloor` [2](#0-1) 

Let `D` be the account's debt (in USD terms of `syntheticToken_`) and `F = debtFloorInUsd`, `m = maxLiquidable`. To succeed, a liquidator must either:

1. Repay everything (`amountToRepay_ = balance`), leaving 0 debt — but `wadDiv(balance) = 1e18 > m` for any `m < 1e18`, so it reverts with `AmountGreaterThanMaxLiquidable`, or
2. Repay `amountToRepay_ ≤ m·balance`, leaving `D·(1 - m) ≤ remaining < D`, which requires `D·(1 - m) ≥ F` to pass the floor check.

Therefore whenever `F < D·(1 - m) < F` fails and `D ≤ F/(1-m)` — i.e. for all `D ∈ (F, F/(1-m))`, which with the deployed default `m = 0.5e18` is `D ∈ (F, 2F)` — **every possible `amountToRepay_` reverts**. `quoteLiquidateMax` confirms the cap is enforced on the quoted side as well [3](#0-2) . `debtFloorInUsd` and `maxLiquidable` are independent governor parameters with no consistency check tying them together (`updateDebtFloor`, `updateMaxLiquidable`) [4](#0-3) .

### Impact Explanation
An account whose synthetic debt falls inside the dead zone and whose position becomes unhealthy cannot be liquidated by anyone, permanently, until its debt grows above `F/(1-m)` through interest accrual or its collateral recovers. An attacker can deliberately open a position with debt just above `F` (the floor only requires `D ≥ F` at issuance; nothing requires `D ≥ F/(1-m)`), let it drift underwater via normal price movement, and the position becomes unseizable bad debt. At scale — many dust-sized positions each just above the floor — this produces protocol insolvency: synthetic liabilities backed by collateral worth less than the debt, with no functioning liquidation path to clear them. This maps directly to the CVE bug class: a bounds assertion that fires for every input of a reachable class, permanently disabling the operation.

### Likelihood Explanation
The dead zone exists on any deployment where `debtFloorInUsd > 0` and `maxLiquidable < 1e18` — the default `maxLiquidable` set in `initialize` is 50% [5](#0-4) . Exploitation requires only an unprivileged account opening a position (public `deposit` + `DebtToken` issue path) sized in the `(F, 2F)` band and waiting for/adjusting to an unhealthy state — e.g., by withdrawing unlocked collateral after interest accrual pushes debt up. No privileged roles, oracle misbehavior, or timing is needed. Note: whether `DebtToken.issue`/`mint` independently enforces the floor at issuance could not be fully verified within this review; if minting requires `D ≥ F` only (not `D ≥ F/(1-m)`), the dead zone is reachable. Also, because `seize` calls `_transfer` which calls `pool.addToDepositTokensOfAccount(recipient)` [6](#0-5) , the same revert-on-bound pattern additionally fires if the liquidator or fee collector is at `MAX_TOKENS_PER_USER` — a secondary, narrower DoS of the same class.

### Recommendation
Allow full repayment (`amountToRepay_ == _debtTokenBalance`) to bypass the `maxLiquidable` cap so a position can always be closed out entirely, e.g.:

```solidity
// contracts/Pool.sol
if (amountToRepay_ != _debtTokenBalance && amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
    revert AmountGreaterThanMaxLiquidable();
}
```

Alternatively/additionally, enforce at debt-issuance time that new debt is either 0 or `≥ debtFloorInUsd / (1 - maxLiquidable)`, so the dead zone can never be entered. Both checks are governor-tunable, so the constraint should be derived dynamically rather than assumed.

### Proof of Concept
Hardhat fork outline (fill in deployed addresses from `deployments/`):

```ts
// Fork e.g. Base at a block where Pool has debtFloorInUsd = F > 0 and maxLiquidable = 0.5e18.
const pool = await ethers.getContractAt('Pool', POOL_ADDRESS);
const msd = await ethers.getContractAt('DepositToken', MSD_TOKEN);
const debt = await ethers.getContractAt('DebtToken', DEBT_TOKEN);
const synth = await ethers.getContractAt('SyntheticToken', SYNTH);

// 1. Attacker deposits collateral and mints synthetic so that debt in USD is in (F, 2F).
await msd.deposit(collateralAmount, attacker.address);
await debt.issue(synthAmount /* ≈ 1.5 * F in USD */, attacker.address);

// 2. Push the position underwater: withdraw all unlocked collateral
//    (unlockedBalanceOf allows withdrawing the excess over the issuable limit),
//    or wait for/let interest accrue so debtPositionOf(attacker).isHealthy == false.
//    Alternatively on a fork, move the oracle price down within allowed update bounds.
await msd.withdraw(await msd.unlockedBalanceOf(attacker.address), attacker.address);

const [healthy] = await pool.debtPositionOf(attacker.address);
expect(healthy).to.be.false;

// 3. Every liquidation attempt reverts.
const bal = await debt.balanceOf(attacker.address);
// Full repay -> exceeds 50% cap
await expect(pool.connect(liquidator).liquidate(synth.address, attacker.address, bal, msd.address))
  .revertedWithCustomError(pool, 'AmountGreaterThanMaxLiquidable');
// Max allowed repay (50%) -> leaves ~0.75*F < F remaining debt
await expect(pool.connect(liquidator).liquidate(synth.address, attacker.address, bal.div(2), msd.address))
  .revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');
// quoteLiquidateMax returns the capped amount that always trips the floor
const maxRepay = await pool.quoteLiquidateMax(synth.address, attacker.address, msd.address);
await expect(pool.connect(liquidator).liquidate(synth.address, attacker.address, maxRepay, msd.address))
  .revertedWithCustomError(pool, 'RemainingDebtIsLowerThanTheFloor');
```

Expected result: all three calls revert; the underwater position is permanently unliquidatable until debt grows past `F/(1-maxLiquidable)` or governance intervenes — demonstrating assertion-driven DoS of the liquidation path leading to accruable bad debt.

### Citations

**File:** contracts/Pool.sol (L181-181)
```text
        maxLiquidable = 0.5e18; // 50%
```

**File:** contracts/Pool.sol (L435-439)
```text
        _maxAmountToRepay = debtTokenOf[syntheticToken_].balanceOf(account_).wadMul(maxLiquidable);

        if (_amountToRepay < _maxAmountToRepay) {
            _maxAmountToRepay = _amountToRepay;
        }
```

**File:** contracts/Pool.sol (L567-569)
```text
        if (amountToRepay_.wadDiv(_debtTokenBalance) > maxLiquidable) {
            revert AmountGreaterThanMaxLiquidable();
        }
```

**File:** contracts/Pool.sol (L571-579)
```text
        if (debtFloorInUsd > 0) {
            uint256 _newDebtInUsd = masterOracle().quoteTokenToUsd(
                address(syntheticToken_),
                _debtTokenBalance - amountToRepay_
            );
            if (_newDebtInUsd > 0 && _newDebtInUsd < debtFloorInUsd) {
                revert RemainingDebtIsLowerThanTheFloor();
            }
        }
```

**File:** contracts/Pool.sol (L768-784)
```text
    function updateDebtFloor(uint256 newDebtFloorInUsd_) external onlyGovernor {
        uint256 _currentDebtFloorInUsd = debtFloorInUsd;
        if (newDebtFloorInUsd_ == _currentDebtFloorInUsd) revert NewValueIsSameAsCurrent();
        emit DebtFloorUpdated(_currentDebtFloorInUsd, newDebtFloorInUsd_);
        debtFloorInUsd = newDebtFloorInUsd_;
    }

    /**
     * @notice Update maxLiquidable (liquidation cap)
     */
    function updateMaxLiquidable(uint256 newMaxLiquidable_) external onlyGovernor {
        if (newMaxLiquidable_ > 1e18) revert MaxLiquidableTooHigh();
        uint256 _currentMaxLiquidable = maxLiquidable;
        if (newMaxLiquidable_ == _currentMaxLiquidable) revert NewValueIsSameAsCurrent();
        emit MaxLiquidableUpdated(_currentMaxLiquidable, newMaxLiquidable_);
        maxLiquidable = newMaxLiquidable_;
    }
```

**File:** contracts/DepositToken.sol (L518-520)
```text
        if (_recipientBalanceBefore == 0 && amount_ > 0) {
            pool.addToDepositTokensOfAccount(recipient_);
        }
```
