### Title
Interest rate/fee config changes apply retroactively to the entire elapsed accrual period instead of being segmented - (File: `programs/marginfi/src/state/bank.rs`)

### Summary
`Bank::accrue_interest()` computes the interest and fee accrual over the *entire* elapsed `time_delta` since `last_update` using whatever `interest_rate_config` is currently stored on the bank at the moment `accrue_interest` is called. If the interest-rate/fee configuration is changed via `lending_pool_configure_bank` or `lending_pool_configure_bank_interest_only` while interest has not yet been accrued (i.e., `last_update` is stale), the subsequent accrual applies the *new* rates and fee parameters across the *whole* stale period, including time that elapsed before the config change — exactly the same bug class as the TermMax `_accruedPeriodInterest()` report, where the latest `performanceFeeRate` was applied to the entire period regardless of when it changed.

### Finding Description
`accrue_interest` in `programs/marginfi/src/state/bank.rs` computes: [1](#0-0) 
then builds the interest-rate calculator from the bank's *current* config and applies it over the full `time_delta`: [2](#0-1) 

The calculator itself computes lending/borrowing APRs and the insurance/group/protocol fee APRs, then multiplies each by `time_delta` in `calc_interest_rate_accrual_state_changes` / `calc_interest_payment_for_period`: [3](#0-2) [4](#0-3) 

There is no mechanism to split `time_delta` into sub-periods bounded by rate-config changes; the whole elapsed period is charged at whatever config is active when `accrue_interest` finally runs.

Crucially, the instructions that change the interest-rate/fee configuration do **not** force interest to be accrued (settled) first with the old rates:
- `lending_pool_configure_bank` directly calls `bank.configure(&bank_config)`, which mutates `self.config.interest_rate_config` in place, with no preceding `accrue_interest` call: [5](#0-4) [6](#0-5) 
- `lending_pool_configure_bank_interest_only` (callable by the scoped `delegate_curve_admin` role) likewise mutates `interest_rate_config` directly without accruing first: [7](#0-6) 

Because `accrue_interest` only runs lazily (triggered permissionlessly, or as a side effect of user deposit/withdraw/borrow/repay/liquidation instructions), a bank can easily accumulate a stale `time_delta` (minutes to days, depending on activity) before any config change takes effect and before the next accrual runs. When accrual finally executes, it uses the *new* config for the *entire* stale window, not just the portion after the change — misallocating interest between borrowers and lenders and misallocating `insurance_fees_collected`, `group_fees_collected`, and `protocol_fees_collected` for the pre-change portion of the period.

### Impact Explanation
This breaks the financial accuracy guarantee of interest/fee accrual for unprivileged depositors and borrowers on affected banks:
- Borrowers can be over/under-charged interest for time that occurred under the old rate curve/fee parameters.
- Lenders' `asset_share_value` growth (their yield) is computed with the wrong rate for part of the period.
- `collected_insurance_fees_outstanding` / `collected_group_fees_outstanding` / `collected_program_fees_outstanding` (core fee-accounting state, per `guides/ADMIN/COLLECTING_FEES.md`) are also miscalculated for the pre-change portion of the period, corrupting protocol/insurance/group revenue accounting.
- This is a core accounting-path bug (affects `asset_share_value`/`liability_share_value`, which every account's health and every user's balance depend on), so miscalculation can propagate into incorrect user balances, incorrect fee vault amounts, and in aggregate, insolvency/bad-debt risk if systematically exploited (e.g., timing borrows/repayments/config changes to benefit from stale-period mispricing).

Impact is assessed as **Medium**, consistent with the original report’s severity rating, because it affects financial fairness/accounting integrity for unprivileged users rather than causing an immediate, deterministic loss of principal in a single transaction.

### Likelihood Explanation
**Medium.** The trigger (an interest-rate/fee config change) is admin/`delegate_curve_admin`-gated, but:
- It requires no special timing or race condition — any config change that lands while a bank has a non-trivial time gap since its last accrual (which is common for less-active banks, per the project's own documentation: "Less popular Banks might compound just a few times per week") will always mis-price the entire stale window.
- The impact lands on unprivileged depositors/borrowers of the affected bank, not on the admin.
- No code path forces an `accrue_interest()` settlement before a rate/fee parameter change takes effect, so this occurs on essentially every configuration update where the bank hasn't just been accrued.

### Recommendation
Mirror the TermMax fix: force interest to accrue and settle with the *old* configuration immediately before applying a new `interest_rate_config`/fee configuration, e.g., call `bank.accrue_interest(...)` at the start of `lending_pool_configure_bank` and `lending_pool_configure_bank_interest_only` (as is already done, for example, in `lending_pool_emissions_deposit`, see `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs:117-122`) before mutating `self.config.interest_rate_config`. This ensures `last_update` is bumped and all interest/fees up to the moment of the config change are computed with the pre-change parameters, so the segment boundary is properly respected and no retroactive re-pricing of already-elapsed time occurs.

### Proof of Concept
Conceptual reproduction (mirroring the TermMax PoC), using existing project instructions:
1. Deposit and borrow to create non-zero `total_asset_shares`/`total_liability_shares` on a bank (see `programs/marginfi/tests/admin_actions/interest_accrual.rs` for the harness pattern).
2. Let `time_delta_1` seconds elapse without calling `LendingPoolAccrueBankInterest` or any instruction that triggers `bank.accrue_interest`.
3. As `delegate_curve_admin`, call `lending_pool_configure_bank_interest_only` (`programs/marginfi/src/instructions/marginfi_group/configure_bank_lite.rs:12-34`) to change `zero_util_rate`/`hundred_util_rate`/`points`/fee parameters.
4. Let `time_delta_2` seconds elapse.
5. Call `LendingPoolAccrueBankInterest` (`programs/marginfi/src/instructions/marginfi_group/accrue_bank_interest.rs:8-26`).
6. Observe that `bank.accrue_interest` uses only the new config and applies it to the full `time_delta_1 + time_delta_2`, rather than applying the old config to `time_delta_1` and the new config to `time_delta_2` — confirmed by the accrual logic at `programs/marginfi/src/state/bank.rs:546-564` and `programs/marginfi/src/state/interest_rate.rs:425-486`, which take only a single `time_delta` and a single `InterestRateCalc` snapshot.

Note: I was not able to find an existing automated test in the indexed codebase that specifically exercises "config change mid-accrual-period," so this analysis is based on tracing the code paths directly; a Devin session with full repo/test access could add a concrete Rust test (similar to `programs/marginfi/tests/admin_actions/interest_accrual.rs`) to empirically confirm the miscalculated fee/interest split.

### Citations

**File:** programs/marginfi/src/state/bank.rs (L441-443)
```rust
        if let Some(ir_config) = &config.interest_rate_config {
            self.config.interest_rate_config.update(ir_config);
        }
```

**File:** programs/marginfi/src/state/bank.rs (L520-528)
```rust
        let time_delta: u64 = (current_timestamp - self.last_update).try_into().unwrap();
        if time_delta == 0 {
            return Ok(());
        }

        let total_assets = self.get_asset_amount(self.total_asset_shares.into())?;
        let total_liabilities = self.get_liability_amount(self.total_liability_shares.into())?;

        self.last_update = current_timestamp;
```

**File:** programs/marginfi/src/state/bank.rs (L546-564)
```rust
        let ir_calc = self
            .config
            .interest_rate_config
            .create_interest_rate_calculator(group);

        let InterestRateStateChanges {
            new_asset_share_value: asset_share_value,
            new_liability_share_value: liability_share_value,
            insurance_fees_collected,
            group_fees_collected,
            protocol_fees_collected,
        } = calc_interest_rate_accrual_state_changes(
            time_delta,
            total_assets,
            total_liabilities,
            &ir_calc,
            self.asset_share_value.into(),
            self.liability_share_value.into(),
        )?;
```

**File:** programs/marginfi/src/state/interest_rate.rs (L383-396)
```rust
/// Calculates the interest payment for a given period `time_delta` in a principal value `value` for interest rate (in APR) `arp`.
/// Result is the interest payment.
fn calc_interest_payment_for_period(apr: I80F48, time_delta: u64, value: I80F48) -> Option<I80F48> {
    if apr.is_zero() {
        return Some(I80F48::ZERO);
    }

    let interest_payment: I80F48 = value
        .checked_mul(apr)?
        .checked_mul(time_delta.into())?
        .checked_div(SECONDS_PER_YEAR)?;

    Some(interest_payment)
}
```

**File:** programs/marginfi/src/state/interest_rate.rs (L425-486)
```rust
pub fn calc_interest_rate_accrual_state_changes(
    time_delta: u64,
    total_assets_amount: I80F48,
    total_liabilities_amount: I80F48,
    interest_rate_calc: &InterestRateCalc,
    asset_share_value: I80F48,
    liability_share_value: I80F48,
) -> MarginfiResult<InterestRateStateChanges> {
    // If the cache is empty, we need to calculate the interest rates
    let utilization_rate: I80F48 = total_liabilities_amount
        .checked_div(total_assets_amount)
        .ok_or_else(math_error!())?;
    debug!(
        "Utilization rate: {}, time delta {}s",
        utilization_rate, time_delta
    );
    let interest_rates = interest_rate_calc.calc_interest_rate(utilization_rate)?;

    debug!("{:#?}", interest_rates);

    let ComputedInterestRates {
        lending_rate_apr,
        borrowing_rate_apr,
        group_fee_apr,
        insurance_fee_apr,
        protocol_fee_apr,
        ..
    } = interest_rates;

    Ok(InterestRateStateChanges {
        new_asset_share_value: calc_accrued_interest_payment_per_period(
            lending_rate_apr,
            time_delta,
            asset_share_value,
        )
        .ok_or_else(math_error!())?,
        new_liability_share_value: calc_accrued_interest_payment_per_period(
            borrowing_rate_apr,
            time_delta,
            liability_share_value,
        )
        .ok_or_else(math_error!())?,
        insurance_fees_collected: calc_interest_payment_for_period(
            insurance_fee_apr,
            time_delta,
            total_liabilities_amount,
        )
        .ok_or_else(math_error!())?,
        group_fees_collected: calc_interest_payment_for_period(
            group_fee_apr,
            time_delta,
            total_liabilities_amount,
        )
        .ok_or_else(math_error!())?,
        protocol_fees_collected: calc_interest_payment_for_period(
            protocol_fee_apr,
            time_delta,
            total_liabilities_amount,
        )
        .ok_or_else(math_error!())?,
    })
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L20-45)
```rust
pub fn lending_pool_configure_bank(
    ctx: Context<LendingPoolConfigureBank>,
    bank_config: BankConfigOpt,
) -> MarginfiResult {
    let mut bank = ctx.accounts.bank.load_mut()?;

    // If settings are frozen, you can only update the deposit and borrow limits, everything else is ignored.
    if bank.get_flag(FREEZE_SETTINGS) {
        bank.configure_unfrozen_fields_only(&bank_config)?;

        msg!("WARN: Only deposit+borrow limits updated. Other settings IGNORED for frozen banks!");

        emit!(LendingPoolBankConfigureFrozenEvent {
            header: GroupEventHeader {
                marginfi_group: ctx.accounts.group.key(),
                signer: Some(*ctx.accounts.admin.key)
            },
            bank: ctx.accounts.bank.key(),
            mint: bank.mint,
            deposit_limit: bank.config.deposit_limit,
            borrow_limit: bank.config.borrow_limit,
        });
    } else {
        // Settings are not frozen, everything updates
        bank.configure(&bank_config)?;
        msg!("Bank configured!");
```

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank_lite.rs (L12-34)
```rust
pub fn lending_pool_configure_bank_interest_only(
    ctx: Context<LendingPoolConfigureBankInterestOnly>,
    interest_rate_config: InterestRateConfigOpt,
) -> MarginfiResult {
    let mut bank = ctx.accounts.bank.load_mut()?;
    msg!(
        "Configuring bank: {:?} mint: {:?}",
        ctx.accounts.bank.key(),
        bank.mint
    );

    // If settings are frozen, interest rates can't update.
    if bank.get_flag(FREEZE_SETTINGS) {
        msg!("WARN: Bank settings frozen, did nothing.");
    } else {
        bank.config
            .interest_rate_config
            .update(&interest_rate_config);
        bank.config.interest_rate_config.validate()?;
        msg!("Bank configured!");
    }

    Ok(())
```
