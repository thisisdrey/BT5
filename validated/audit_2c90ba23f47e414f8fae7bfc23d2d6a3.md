### Title
Bankruptcy trigger checks gross asset value instead of net equity, allowing dust deposits to indefinitely block permissionless loss socialization - ([File: programs/marginfi/src/state/marginfi_account.rs])

### Summary
`check_account_bankrupt` gates bankruptcy eligibility on `equity_assets < BANKRUPT_THRESHOLD` (raw asset value, unweighted) rather than net equity (`assets - liabilities`) as documented in `RISK_PARAMETERS.md`. An account owner whose account has been fully liquidated down to near-zero assets but retains a large outstanding liability can defeat this check indefinitely by depositing a small amount of collateral (just enough to push `equity_assets` to ≥ $0.10), causing `lending_pool_handle_bankruptcy` to revert with `AccountNotBankrupt` regardless of how large the liability is.

### Finding Description
`check_account_bankrupt` computes: [1](#0-0) 

```rust
let (equity_assets, equity_liabs) = get_health_components(..., RequirementType::Equity, ...)?;
let has_liabilities = equity_liabs > I80F48::ZERO;
let below_bankruptcy_threshold = equity_assets < BANKRUPT_THRESHOLD;
let liabilities_exceed_assets = equity_liabs > equity_assets;
let is_bankrupt = has_liabilities && below_bankruptcy_threshold && liabilities_exceed_assets;
```

`below_bankruptcy_threshold` tests the raw asset side (`equity_assets`) against `BANKRUPT_THRESHOLD` ($0.10), not the net equity (`equity_assets - equity_liabs`) as `RISK_PARAMETERS.md` states ("**Equity < $0.10**: Account is eligible for bankruptcy handling", where Equity = `sum(asset_value) - sum(debt_value)`). [2](#0-1) 

This means an account with, e.g., $0.15 in assets and $500,000 in liabilities has `liabilities_exceed_assets = true` and `has_liabilities = true`, but `below_bankruptcy_threshold = false`, so `is_bankrupt` evaluates false and `lending_pool_handle_bankruptcy` unconditionally fails with `AccountNotBankrupt`: [3](#0-2) 

The intended flow per `BANKRUPTCY.md` is that liquidators fully drain the account's assets to ~0 before bankruptcy handling runs, and the $0.10 threshold exists purely to tolerate rounding dust: [4](#0-3) 

Once `EndLiquidation` has cleared `ACCOUNT_IN_RECEIVERSHIP` (required precondition for `LendingPoolHandleBankruptcy`, enforced via its account constraint), the account is a normal active account and its owner can freely call the standard deposit instruction to add new collateral — deposits are risk-reducing and are not gated by health checks. By depositing a small amount (just over $0.10 of value), the account owner raises `equity_assets` above the threshold, permanently defeating the bankruptcy check for that account regardless of the magnitude of its liabilities, until the added dust collateral is itself consumed by a fresh liquidation (an action with negligible/no economic incentive for a liquidator, since fees are proportional to a near-zero seized amount).

### Impact Explanation
This lets an underwater account owner (or any actor who controls the account) indefinitely delay `lending_pool_handle_bankruptcy` from succeeding at effectively no ongoing cost (a single small deposit), while the outstanding liability continues to accrue interest via `bank.accrue_interest`. Because bankruptcy handling is the only mechanism that repays bad debt from the insurance fund and/or socializes losses to remaining depositors, blocking it means that unresolved bad debt (and its growing interest) remains on the books, increasing the eventual haircut socialized to depositors of that bank versus an eagerly-processed baseline. This matches `BANKRUPTCY.md`'s own acknowledged consequence ("If Bankruptcy isn't executed on a bankrupt user, then remaining depositors can never withdraw the whole balance in the bank"), but here the non-execution is attacker-controlled rather than incidental, and the amount of bad debt that ultimately gets socialized grows unboundedly with time. [5](#0-4) 

### Likelihood Explanation
Feasible for any unprivileged account owner with an account that has already been liquidated down to near-zero collateral but retains a debt balance (a realistic post-liquidation state). No admin/governance access is required — only ownership of the marginfi account and the ability to submit a standard deposit transaction ahead of (or interleaved with) a permissionless `lending_pool_handle_bankruptcy` call. The only counter-force is a liquidator choosing to seize the tiny re-deposited collateral, which is economically unattractive at dust-sized values, making the block sustainable at low, repeatable cost to the attacker.

### Recommendation
Change `check_account_bankrupt`'s bankruptcy condition to test net equity (`equity_assets.checked_sub(equity_liabs)... < BANKRUPT_THRESHOLD`, combined with `liabilities_exceed_assets`) instead of gross `equity_assets` alone, matching the documented invariant in `RISK_PARAMETERS.md`. Alternatively, if the "assets must be near zero" semantic is intentional (matching `BANKRUPTCY.md`'s liquidate-to-zero design), restrict deposits into accounts that are already in a deep negative-equity state (e.g., require full liability repayment or a minimum health improvement before allowing further deposits on already-insolvent accounts), or make the bankruptcy check independent of any *new* deposits made after `EndLiquidation` by snapshotting the asset value at the point receivership ends.

### Proof of Concept
Rust integration test plan (building on `programs/marginfi/tests/admin_actions/bankruptcy.rs` patterns):
1. Set up a bank with an LP depositor and a borrower account; drive the borrower's collateral price down (or use `nullify_assets_for_bank`-style helper) so the account has liabilities >> assets.
2. Fully liquidate the account via classic/receivership liquidation until its asset balance is ~0 and `ACCOUNT_IN_RECEIVERSHIP` is cleared (call `EndLiquidation`).
3. Assert `try_handle_bankruptcy` now succeeds (baseline, matching existing `marginfi_group_handle_bankruptcy_*` tests).
4. Repeat steps 1-2, but before calling `try_handle_bankruptcy`, have the borrower deposit a dust amount (e.g., $0.15 equivalent) into the account.
5. Assert `try_handle_bankruptcy` now fails with `MarginfiError::AccountNotBankrupt`, even though `liability_value > asset_value` by many orders of magnitude.
6. Advance the clock/accrue interest multiple periods, re-deposit dust each cycle if the liquidator does not intervene, and confirm the account remains blocked from bankruptcy indefinitely while `bank.accrue_interest` grows the liability.
7. Compare final socialized-loss amount (once bankruptcy is eventually forced by nullifying assets outright) against a baseline where bankruptcy was processed immediately after the initial liquidation, quantifying the extra bad debt accrued due to the delay.

### Citations

**File:** programs/marginfi/src/state/marginfi_account.rs (L978-996)
```rust
    let (equity_assets, equity_liabs) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Equity,
        health_cache,
        HealthPriceMode::Live { liq_cache: None },
    )?;

    let has_liabilities = equity_liabs > I80F48::ZERO;
    let below_bankruptcy_threshold = equity_assets < BANKRUPT_THRESHOLD;
    let liabilities_exceed_assets = equity_liabs > equity_assets;
    let is_bankrupt = has_liabilities && below_bankruptcy_threshold && liabilities_exceed_assets;

    if !is_bankrupt {
        return err!(MarginfiError::AccountNotBankrupt);
    }

    Ok(())
}
```

**File:** guides/RISK_AND_LIQUIDATORS/RISK_PARAMETERS.md (L35-40)
```markdown
Equity          = sum(asset_value)                      - sum(debt_value)  [no weights]
```

- **Initial Health >= 0**: Required to open new borrows or withdraw collateral.
- **Maint Health < 0**: Account is eligible for liquidation.
- **Equity < $0.10**: Account is eligible for bankruptcy handling.
```

**File:** programs/marginfi/src/instructions/marginfi_group/handle_bankruptcy.rs (L80-84)
```rust
    check_account_bankrupt(
        &marginfi_account,
        ctx.remaining_accounts,
        &mut Some(&mut health_cache),
    )?;
```

**File:** guides/RISK_AND_LIQUIDATORS/BANKRUPTCY.md (L27-34)
```markdown
## Discharging a Bankruptcy

First, liquidators consume all the remaining assets that the user has. If the user has A dollars in
assets and B dollars in liabilities (in equity value, i.e. excluding any weights), we know that B >
A. After liquidation is complete, A_new = 0, and B_new = B - A + X, where X is the liquidation
premium and insurance.

Run `collect_bank_fees` before beginning the next step so the insurance fund is fully capitalized.
```

**File:** guides/RISK_AND_LIQUIDATORS/BANKRUPTCY.md (L50-54)
```markdown
### What Happens if it Doesn't Run?

If Bankruptcy isn't executed on a bankrupt user, then remaining depositors can never withdraw the
whole balance in the bank. The last few depositors who try to withdraw will find there are not
enough funds - proportional to the liabilities held by bankrupt users.
```
