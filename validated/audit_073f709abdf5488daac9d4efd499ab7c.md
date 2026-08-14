Based on the analysis, I've confirmed the analog exists in the marginfi-v2 codebase.

### Title
Inaccurate Interest/Fee Accrual When Bank Interest Rate Config Is Updated Without First Accruing Pending Interest - (File: `programs/marginfi/src/state/bank.rs`, `programs/marginfi/src/instructions/marginfi_group/configure_bank.rs`, `configure_bank_lite.rs`)

### Summary
`lending_pool_configure_bank`, `lending_pool_configure_bank_interest_only`, and the `ConfigureBankLiteCurve` path all mutate `bank.config.interest_rate_config` (base-rate curve points and the insurance/group/protocol fee rates) directly, without first invoking `accrue_interest` to settle interest owed under the previous rate configuration for the time elapsed since `last_update`.

### Finding Description
`accrue_interest` in `programs/marginfi/src/state/bank.rs` computes `time_delta = current_timestamp - self.last_update` and then applies the *currently stored* `interest_rate_config` (via `create_interest_rate_calculator`) across that *entire* `time_delta` to compute `asset_share_value`/`liability_share_value` changes and the three outstanding fee buckets (`collected_insurance_fees_outstanding`, `collected_group_fees_outstanding`, `collected_program_fees_outstanding`): [1](#0-0) 

Crucially, `accrue_interest` is only invoked lazily, at the start of the next user-driven action (borrow/repay/withdraw/deposit) or via the permissionless `lending_pool_accrue_bank_interest` instruction: [2](#0-1) 

However, `lending_pool_configure_bank` (`configure_bank.rs`) calls `bank.configure(&bank_config)`, which directly overwrites `self.config.interest_rate_config` via `InterestRateConfigImpl::update` with no preceding `accrue_interest` call: [3](#0-2) [4](#0-3) 

Likewise `lending_pool_configure_bank_interest_only` (delegate curve admin path) updates `interest_rate_config` with no accrual step: [5](#0-4) 

Because `last_update` is not advanced by the configure instructions, the next time any transaction calls `accrue_interest`, the entire un-accrued time window — including all time that elapsed *before* the config change, when the *old* rates were actually in effect — gets priced using the *new* `interest_rate_config`. This silently and retroactively re-prices interest/fees for a period during which a different rate schedule should have applied, exactly mirroring the reported bug class ("Keeper fee might have been pending for the last set of management fee params and needs to be claimed before new params are set, else fee not accrued is lost forever").

### Impact Explanation
This corrupts core accounting for every depositor/borrower in the bank, not just the admin:
- If fee/rate params are lowered, the insurance/group/program fee buckets under-collect for the pre-change period — a permanent, unrecoverable loss of protocol/insurance revenue for that window (analogous to the "fee not accrued is lost forever" impact in the reference report).
- If fee/rate params are raised, borrowers/depositors are retroactively charged/credited at the new rate for a period when the old, different rate should have applied — an unauthorized state change to `asset_share_value`/`liability_share_value` that misprices every user's position, potentially causing bad debt or incorrect settlement in an already-borderline account (interacting with health-check, liquidation, and bankruptcy logic that depend on accurate share values).

### Likelihood Explanation
This requires no adversarial coordination beyond normal admin operation cadence: any time a group admin (or `delegate_curve_admin`) updates a bank's interest-rate curve or fee parameters — an expected, routine operational action — and time has elapsed since the bank's `last_update` (which happens whenever the bank has been idle even briefly, since fees only accrue lazily on the next interaction), the mispricing occurs automatically on the next accrual. No special conditions or permissionless-side attack are needed to trigger the divergence; it happens as a byproduct of ordinary parameter updates.

### Recommendation
Before applying any `interest_rate_config` change (in `configure_bank`, `configure_bank_interest_only`/`ConfigureBankLiteCurve`), call `bank.accrue_interest(...)` to settle/checkpoint interest and fees under the currently-active configuration and advance `last_update`, mirroring the recommended fix in the reference report (call the accrual/claim function before updating fee params).

### Proof of Concept
1. Bank has active borrowers and depositors; `last_update = T0` with `interest_rate_config = A` (e.g. `protocol_ir_fee = 1%`).
2. No transaction touches the bank for a period (no lazy accrual triggered).
3. At `T1`, admin calls `lending_pool_configure_bank_interest_only` to change `protocol_ir_fee` to `10%`. This directly overwrites `bank.config.interest_rate_config` per `configure_bank_lite.rs` lines 27-29, without accruing interest for `[T0, T1]`. `last_update` remains `T0`.
4. At `T2`, a user calls `lending_account_borrow`/`repay`/`withdraw`, which triggers `bank.accrue_interest(T2, ...)` per `borrow.rs` lines 76-81. Because `last_update` is still `T0`, `time_delta = T2 - T0` (spanning both the old-rate period `[T0,T1]` and new-rate period `[T1,T2]`) is computed entirely using the config as of `T2` (10%), not the config that was actually in force during `[T0,T1]` (1%).
5. Result: `collected_protocol_fees_outstanding` (and share values) reflect the wrong rate for the `[T0,T1]` window — the protocol either under-collects (permanent fee loss) or borrowers are overcharged, depending on the direction of the change — with no mechanism to correct it after the fact. [6](#0-5) [7](#0-6)

### Citations

**File:** programs/marginfi/src/state/bank.rs (L441-443)
```rust
        if let Some(ir_config) = &config.interest_rate_config {
            self.config.interest_rate_config.update(ir_config);
        }
```

**File:** programs/marginfi/src/state/bank.rs (L511-549)
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
