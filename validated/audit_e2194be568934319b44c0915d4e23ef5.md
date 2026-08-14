### Title
Bank interest rate config changes are applied retroactively without accruing interest first, enabling yield theft via front-running - ([File: programs/marginfi/src/instructions/marginfi_group/configure_bank.rs], [File: programs/marginfi/src/instructions/marginfi_group/configure_bank_lite.rs], [File: programs/marginfi/src/state/bank.rs])

### Summary
`lending_pool_configure_bank` and `lending_pool_configure_bank_interest_only` update a `Bank`'s `interest_rate_config` in place without first calling `accrue_interest()` to settle interest for the time elapsed under the *old* config. Because `accrue_interest()` always computes interest for the entire `time_delta` since `last_update` using whatever `interest_rate_config` is active *at the time of the next accrual*, any interest owed for the period before a config change is silently recalculated at the *new* rate. This mirrors the reported Ethos `ReaperVaultV2.setLockedProfitDegradation()` bug class: mutate a rate parameter without first checkpointing state that depends on `(now - last_update)`.

### Finding Description
`configure()` directly mutates `self.config.interest_rate_config` via `self.config.interest_rate_config.update(ir_config)`: [1](#0-0) 

Likewise, `lending_pool_configure_bank_interest_only` mutates the interest rate config directly with no prior accrual: [2](#0-1) 

Neither of these entrypoints invoke `bank.accrue_interest(...)` before applying the new config, unlike `lending_pool_accrue_bank_interest` and every balance-changing instruction (deposit/borrow/withdraw/repay), which always call `accrue_interest` first: [3](#0-2) 

`accrue_interest` computes the entire elapsed `time_delta` since `self.last_update` using the interest rate calculator built from `self.config.interest_rate_config` — whatever that config is *at accrual time*, not what it was during each sub-period of the elapsed time: [4](#0-3) 

Consequently, if `last_update` is stale (e.g. an unpopular bank that hasn't been touched recently) and the admin updates `interest_rate_config` (e.g. via `lending_pool_configure_bank_interest_only`, which is callable by a `delegate_curve_admin`), the entire stale period gets retroactively priced at the new curve the moment the next `accrue_interest` runs. Since `asset_share_value`/`liability_share_value` are single scalars multiplied by *all* shares outstanding at accrual time, an attacker can:
1. Observe/anticipate an interest-rate-config update transaction (increasing the effective yield curve).
2. Front-run it (or simply act quickly right after) with a large `lending_account_deposit`, minting shares at the pre-accrual share value.
3. Trigger (or wait for) the next `accrue_interest`, which applies the new, more favorable curve retroactively across the entire stale `time_delta`, inflating `asset_share_value` for all shares — including the attacker's newly-minted, disproportionately large shareholding.
4. Withdraw immediately, capturing yield that should have accrued to existing depositors under the old curve for that period, diluting their rightful share of the interest for that period.

The reverse (rate lowered) analogously lets a large borrower avoid interest that should have accrued at the old (higher) rate for the stale period by borrowing before the config change lands and letting the lower rate apply retroactively.

### Impact Explanation
This allows an unprivileged, ordinary depositor/borrower to capture value that rightfully belongs to existing depositors/the protocol by exploiting the timing of a legitimate, otherwise-benign admin rate-curve update. This is a concrete theft/unfair-redistribution vector matching the accepted analog class (Medium severity per the original C4 judge), not a privileged-admin exploit — the admin's action is legitimate governance, and the attacker is any unprivileged user who transacts opportunistically around it. Severity is bounded by: (a) the bank must have a stale `last_update` (little/no recent activity) for the window to be meaningful, and (b) the attacker needs enough capital relative to the bank's TVL to meaningfully dilute other depositors' retroactive interest.

### Likelihood Explanation
Likelihood is moderate-to-low in practice because `accrue_interest` is permissionless (`lending_pool_accrue_bank_interest`) and is invoked by every deposit/withdraw/borrow/repay, so actively-used banks rarely accumulate a large stale window. However, less-active banks (low borrow demand, illiquid assets) can go a meaningful time without any accrual, and curve/config updates via `lending_pool_configure_bank_interest_only` (delegate curve admin) or `lending_pool_configure_bank` are routine operational actions, not rare emergency actions — increasing the realistic window for this to be triggered.

### Recommendation
Call `bank.accrue_interest(clock.unix_timestamp, &group, ...)` (and `update_bank_cache`) at the start of `lending_pool_configure_bank`, `lending_pool_configure_bank_interest_only`, and any other instruction that mutates `interest_rate_config`, so that all interest owed under the *old* curve is settled and `last_update` is checkpointed before the new curve takes effect for any subsequent period.

### Proof of Concept
1. Bank B has no activity for a long period (`last_update` stale), with existing depositors holding shares.
2. Admin/`delegate_curve_admin` submits `lending_pool_configure_bank_interest_only` raising `hundred_util_rate`/curve points to increase yield.
3. Attacker observes the pending tx and submits a large `lending_account_deposit` into bank B in the same slot/shortly before it lands (or immediately after, before anyone else transacts).
4. Attacker (or anyone) then triggers `lending_pool_accrue_bank_interest`, or performs any bank operation, which calls `bank.accrue_interest` using the *new* config over the entire stale `time_delta` — inflating `asset_share_value` retroactively for the whole stale period.
5. Attacker withdraws immediately, having captured a disproportionate share of interest that should have accrued under the old, less-favorable curve to the long-standing depositors for that stale period.

### Citations

**File:** programs/marginfi/src/state/bank.rs (L441-443)
```rust
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

**File:** programs/marginfi/src/instructions/marginfi_group/configure_bank_lite.rs (L12-32)
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
```

**File:** programs/marginfi/src/instructions/marginfi_account/deposit.rs (L69-74)
```rust
    bank.accrue_interest(
        clock.unix_timestamp,
        &group,
        #[cfg(not(feature = "client"))]
        bank_loader.key(),
    )?;
```
