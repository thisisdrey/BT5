### Title
Interest rate config changes are not settled against `last_update` before applying new rates, causing retroactive misapplication of interest rates - ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs])

### Summary
Marginfi's per-bank interest accrual model tracks a `last_update` timestamp on the `Bank` account and computes `time_delta = current_timestamp - last_update` whenever `accrue_interest()` runs, then applies the bank's *current* `interest_rate_config` over that entire `time_delta` [1](#0-0) . Both `lending_pool_configure_bank` and `lending_pool_configure_bank_interest_only`, the instructions used to change a bank's `interest_rate_config`, mutate the stored config directly without first calling `accrue_interest()` to settle/checkpoint the bank's state up to the moment of the change [2](#0-1) [3](#0-2) . This is the exact bug class described in the SophonFarming `setStartBlock()` report: a parameter that governs reward/interest accrual is changed by a privileged caller, but the state used to bound "how far back accrual applies" (`lastRewardBlock` / `last_update`) is never advanced/settled, so the new parameter gets applied retroactively over a period during which the old parameter should have governed.

### Finding Description
`Bank::accrue_interest()` uses `self.last_update` as the anchor point for computing how much interest has accrued since the last accrual, and pulls the interest rate curve from `self.config.interest_rate_config` at the time it runs — not from whatever config was active during each sub-interval of `time_delta` [4](#0-3) .

When an admin (group `admin` or `delegate_curve_admin`) calls `lending_pool_configure_bank` or `lending_pool_configure_bank_interest_only` to update the interest curve (e.g., `zero_util_rate`, `hundred_util_rate`, `points`, fee parameters), the code path goes straight to `bank.configure(&bank_config)` / `bank.config.interest_rate_config.update(...)` and writes the new config to the account [5](#0-4) [6](#0-5) . Neither instruction calls `bank.accrue_interest()` beforehand, unlike other bank-mutating instructions such as `lending_account_repay` and `lending_account_borrow`, which explicitly accrue interest first [7](#0-6) [8](#0-7) , or `lending_pool_emissions_deposit`, which also accrues interest before mutating share value [9](#0-8) .

As a result, `last_update` is left pointing to whatever it was before the config change. The next time any user (unprivileged depositor/borrower) triggers accrual — via `deposit`, `withdraw`, `borrow`, `repay`, or the permissionless `lending_pool_accrue_bank_interest` instruction — `time_delta` spans back across the config-change boundary, and the *entire* elapsed period (including time that occurred under the old rate curve) is charged/paid using the *new* curve [10](#0-9) .

### Impact Explanation
Depending on the direction of the parameter change relative to the old curve, this causes either:
1. Lenders/borrowers being retroactively charged or credited interest rates that never should have applied for part of the elapsed period, distorting the fee/insurance/protocol fee splits that are computed from the same misapplied rate [11](#0-10) .
2. Systematic mispricing of `asset_share_value`/`liability_share_value` that directly determines how much unprivileged depositors can withdraw and how much borrowers owe, which is core lending-protocol accounting and can produce a shortfall between assets and liabilities (bad debt) if borrowers are undercharged, or unfairly penalize depositors/borrowers if overcharged.

This affects the core accrual and share-value accounting path used by every bank and every user position, matching the accepted impact categories (unauthorized state change / insolvency risk in accounting) rather than being purely cosmetic.

### Likelihood Explanation
The trigger is a normal, expected admin operation — updating a bank's interest curve is a routine risk-management action described in the README's curve-tuning documentation, not an attack requiring compromised keys [12](#0-11) . Any bank with a non-zero utilization at the time of a curve update, followed by any user transaction or the permissionless accrue instruction, will trigger the retroactive misapplication. No exotic conditions are needed beyond "admin updates rates on an active bank," and the effect compounds with `time_delta` if the bank goes unaccrued for a while before/after the change.

### Recommendation
In both `lending_pool_configure_bank` (programs/marginfi/src/instructions/marginfi_group/configure_bank.rs) and `lending_pool_configure_bank_interest_only` (programs/marginfi/src/instructions/marginfi_group/configure_bank_lite.rs), call `bank.accrue_interest(clock.unix_timestamp, &group, ...)` (and ideally `bank.update_bank_cache(&group)`) to settle interest accrual under the *old* config and advance `last_update` to `now` before applying the new `interest_rate_config`/`BankConfigOpt`. This ensures the new curve only governs time going forward, mirroring the fix pattern recommended for the SophonFarming `setStartBlock()` issue (settle/checkpoint existing accrual state before changing the parameter that governs future accrual).

### Proof of Concept
1. Create a bank with non-zero deposits and borrows (non-zero utilization) and let some time pass without triggering `accrue_interest` (e.g., `last_update` is `T0`).
2. At time `T1` (`T1 > T0`), admin calls `lending_pool_configure_bank_interest_only` to sharply increase `hundred_util_rate` (or otherwise change the curve) — this only mutates `bank.config.interest_rate_config`; `bank.last_update` remains `T0` [3](#0-2) .
3. At time `T2` (`T2 > T1`), any user calls `deposit`/`withdraw`/`borrow`/`repay`, or anyone calls the permissionless `lending_pool_accrue_bank_interest` [13](#0-12) .
4. `accrue_interest` computes `time_delta = T2 - T0` and applies the *new* (post-`T1`) interest curve over the full `T0→T2` window, including the `T0→T1` interval during which the old curve should have applied [14](#0-13) .
5. Comparing expected interest (old curve for `T0→T1`, new curve for `T1→T2`) versus actual interest (new curve for the entire `T0→T2`) shows a measurable discrepancy in `asset_share_value`/`liability_share_value`, proving retroactive misapplication of the rate change.

### Citations

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

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank.rs (L94-122)
```rust
    let clock = Clock::get()?;
    let mut bank = ctx.accounts.bank.load_mut()?;
    let group = ctx.accounts.group.load()?;

    utils::validate_bank_state(&bank, utils::InstructionKind::FailsIfPausedOrReduceState)?;

    // Reject mints with non-zero transfer fees or active transfer hooks.
    let mint_ai = ctx.accounts.mint.to_account_info();
    check!(
        !utils::nonzero_fee(mint_ai.clone(), clock.epoch)?,
        MarginfiError::InvalidTransfer
    );
    check!(
        !utils::has_transfer_hook(mint_ai)?,
        MarginfiError::InvalidTransfer
    );

    let total_asset_shares = I80F48::from(bank.total_asset_shares);
    check!(
        total_asset_shares > I80F48::ZERO,
        MarginfiError::EmissionsUpdateError
    );

    bank.accrue_interest(
        clock.unix_timestamp,
        &group,
        #[cfg(not(feature = "client"))]
        ctx.accounts.bank.key(),
    )?;
```

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

**File:** programs/marginfi/src/instructions/marginfi_account/repay.rs (L67-76)
```rust
    let mut bank = bank_loader.load_mut()?;
    validate_bank_state(&bank, InstructionKind::FailsInPausedState)?;

    let group = marginfi_group_loader.load()?;
    bank.accrue_interest(
        clock.unix_timestamp,
        &group,
        #[cfg(not(feature = "client"))]
        bank_loader.key(),
    )?;
```

**File:** programs/marginfi/src/instructions/marginfi_account/borrow.rs (L76-81)
```rust
    bank_loader.load_mut()?.accrue_interest(
        clock.unix_timestamp,
        &group,
        #[cfg(not(feature = "client"))]
        bank_loader.key(),
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

**File:** README.md (L122-145)
```markdown
### Curve Details

The "Base Rate" or `r` is the rate linearly interpolated between the two nearest points
$(x_0, y_0)$ and $(x_1, y_1)$, where there is always a point at $(0, y_0)$ and $(100, y_n)$.


$r = (ur-x_0)/(x_1-x_0) * (y_1-y_0) + y_0$


Examples:

If the `zero_util_rate` is 10%, and there is a point at (50%, 100%), and the UR is
currently 25%, then

```
r = (25-0)/(50-0) * (100-10) + 10 = 55%
```

If there is a point at (50%, 100%) and (80%, 150%), and the UR is currently 60%,
then

```
r = (60-50)/(80-50) * (150-100) + 100 = 116.67%
```
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
