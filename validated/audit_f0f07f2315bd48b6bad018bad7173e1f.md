Confirmed: `lending_pool_configure_bank_interest_only` at [1](#0-0)  mutates `bank.config.interest_rate_config` in place without first calling `accrue_interest` to checkpoint the pending period at the old rate. The next call to `accrue_interest` computes `time_delta` as the *entire* elapsed period since `last_update` and applies the *current* (post-change) `InterestRateConfig` to that whole delta, as seen at [2](#0-1)  and the accrual math in [3](#0-2) . This is the exact analog of the C4 finding: a globally-changeable rate parameter retroactively applied to interest that already accrued (in wall-clock time) under the old terms.

### Title
Retroactive application of new interest-rate/fee config to already-elapsed accrual period - (File: `programs/marginfi/src/state/bank.rs`)

### Summary
`lending_pool_configure_bank_interest_only` (`delegate_curve_admin`-gated) and `lending_pool_configure_bank` allow updating a bank's `interest_rate_config` — including `insurance_ir_fee`, `insurance_fee_fixed_apr`, `protocol_ir_fee`, `protocol_fixed_fee_apr`, `zero_util_rate`, `hundred_util_rate`, and curve `points` — at any time, without first accruing interest for the period elapsed under the old config. [1](#0-0) [4](#0-3) 

### Finding Description
`Bank::accrue_interest` computes `time_delta = current_timestamp - self.last_update` and applies a single `InterestRateCalc` (built from the bank's *current* `interest_rate_config` and the *current* group fee state) uniformly across that whole `time_delta`. [5](#0-4)  Because config changes made via `lending_pool_configure_bank_interest_only` or `lending_pool_configure_bank` do not call `accrue_interest` beforehand to "checkpoint" the previously-accrued-but-uncommitted interest, any elapsed wall-clock time between the last accrual and the config change gets retroactively priced at the new rate/fee parameters once accrual finally runs. As documented, quieter banks may only compound "a few times per week", widening this retroactive window. [6](#0-5) 

This mirrors the reported bug class exactly: lenders/borrowers who transacted (deposited/borrowed) under one fee/rate regime end up having interest that already accrued in real time re-priced under a regime they never agreed to, once the admin changes `insurance_ir_fee`/`protocol_ir_fee`/curve parameters and someone later triggers accrual (via any deposit/borrow/withdraw/repay or the permissionless `lending_pool_accrue_bank_interest`). [7](#0-6) 

### Impact Explanation
Impact is economic mispricing rather than direct fund loss: borrowers can be charged more interest/fees than agreed for time already elapsed under the old curve, or lenders can receive less yield than they were owed, depending on the direction of the config change. Because interest for a bank compounds for *all* users simultaneously on the next transaction, [6](#0-5)  a single admin config change can retroactively re-price interest for every position in that bank, not just new activity.

### Likelihood Explanation
Requires a `delegate_curve_admin` (or group admin) config change combined with a bank that hasn't accrued recently — plausible for lower-activity banks, and always possible for the window between the last transaction and the config-changing transaction, since there's no forced accrual on config update.

### Recommendation
Force `accrue_interest` to run (checkpointing `last_update` and committing share-value/fee changes under the old config) as the first step of `lending_pool_configure_bank_interest_only` and `lending_pool_configure_bank` before applying any change to `interest_rate_config`, so that only future time is priced under the new parameters.

### Proof of Concept
1. Bank has `last_update = T0`; `insurance_ir_fee = 5%`.
2. No user transacts with the bank for several days (accrual not triggered).
3. At `T1 = T0 + 5 days`, `delegate_curve_admin` calls `lending_pool_configure_bank_interest_only` setting `insurance_ir_fee = 50%`, which directly mutates `bank.config.interest_rate_config` without accruing first. [8](#0-7) 
4. At `T2 = T1 + 1 second`, any user deposits/borrows/withdraws/repays, triggering `accrue_interest` with `time_delta = T2 - T0` (5 days + 1s), computed entirely with the new 50% `insurance_ir_fee`. [9](#0-8) 
5. All 5 days of interest — most of which occurred while the fee was 5% — are charged/collected at the new 50% rate, harming whichever side (borrowers via higher `liability_share_value`, or the fee recipients getting less/more) was disadvantaged by the timing of the change.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank_lite.rs (L12-35)
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
}
```

**File:** programs/marginfi/src/state/bank.rs (L403-443)
```rust
    fn configure(&mut self, config: &BankConfigOpt) -> MarginfiResult {
        set_if_some!(self.config.asset_weight_init, config.asset_weight_init);
        set_if_some!(self.config.asset_weight_maint, config.asset_weight_maint);
        set_if_some!(
            self.config.liability_weight_init,
            config.liability_weight_init
        );
        set_if_some!(
            self.config.liability_weight_maint,
            config.liability_weight_maint
        );
        set_if_some!(self.config.deposit_limit, config.deposit_limit);

        set_if_some!(self.config.borrow_limit, config.borrow_limit);

        if let Some(new_state) = config.operational_state {
            // JupLend banks must be activated exactly once through `juplend_init_position`.
            check!(
                !(self.config.asset_tag == ASSET_TAG_JUPLEND
                    && self.config.operational_state == BankOperationalState::Uninitialized),
                MarginfiError::Unauthorized
            );
            // These states are unreachable by configuration
            check!(
                new_state != BankOperationalState::KilledByBankruptcy
                    && new_state != BankOperationalState::Uninitialized,
                MarginfiError::Unauthorized
            );
            // Log operational state change
            let old_state = self.config.operational_state;
            self.config.operational_state = new_state;
            msg!(
                "Operational state changed from {:?} to {:?}",
                old_state,
                new_state
            );
        }

        if let Some(ir_config) = &config.interest_rate_config {
            self.config.interest_rate_config.update(ir_config);
        }
```

**File:** programs/marginfi/src/state/bank.rs (L511-564)
```rust
    fn accrue_interest(
        &mut self,
        current_timestamp: i64,
        group: &MarginfiGroup,
        #[cfg(not(feature = "client"))] bank: Pubkey,
    ) -> MarginfiResult<()> {
        #[cfg(all(not(feature = "client"), feature = "debug"))]
        sol_log_compute_units();

        let time_delta: u64 = (current_timestamp - self.last_update).try_into().unwrap();
        if time_delta == 0 {
            return Ok(());
        }

        let total_assets = self.get_asset_amount(self.total_asset_shares.into())?;
        let total_liabilities = self.get_liability_amount(self.total_liability_shares.into())?;

        self.last_update = current_timestamp;

        if (total_assets == I80F48::ZERO) || (total_liabilities == I80F48::ZERO) {
            #[cfg(not(feature = "client"))]
            emit!(LendingPoolBankAccrueInterestEvent {
                header: GroupEventHeader {
                    marginfi_group: self.group,
                    signer: None
                },
                bank,
                mint: self.mint,
                delta: time_delta,
                fees_collected: 0.,
                insurance_collected: 0.,
            });

            return Ok(());
        }
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

**File:** README.md (L176-180)
```markdown
like SOL, compound every few minutes, or even every few seconds on more active days. Less popular
Banks might compound just a few times per week, but these Banks typically have very few borrows (and
thus a low APR to compound). Since interest compounds based on usage, the more popular our platform,
the more often interest compounds. Remember that interest accrues for all of a Bank's users at the
same time: that means if anyone transacts with a bank, the interest for *every* user compounds!
```

**File:** programs/marginfi/src/instructions/marginfi_group/accrue_bank_interest.rs (L8-26)
```rust
pub fn lending_pool_accrue_bank_interest(
    ctx: Context<LendingPoolAccrueBankInterest>,
) -> MarginfiResult {
    let clock = Clock::get()?;
    let mut bank = ctx.accounts.bank.load_mut()?;
    let group = &ctx.accounts.group.load()?;

    bank.accrue_interest(
        clock.unix_timestamp,
        group,
        #[cfg(not(feature = "client"))]
        ctx.accounts.bank.key(),
    )?;

    // TODO see if we can recycle some things like the InterestRateCalc from accrue to save some CU
    bank.update_bank_cache(group)?;

    Ok(())
}
```
