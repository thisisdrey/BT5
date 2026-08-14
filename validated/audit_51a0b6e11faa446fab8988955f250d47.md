Based on my research, I found a concrete structural analog to the reported bug class in the marginfi-v2 receivership liquidation flow.

### Title
`start_liquidation`/`start_deleverage` compute pre-liquidation health and snapshot equity without accruing bank interest, unlike Classic liquidation - ([File: programs/marginfi/src/instructions/marginfi_account/liquidate_start.rs])

### Summary
The permissionless `start_liquidation` instruction (and the permissioned `start_deleverage`) call `start_receivership`, which determines whether an account is liquidatable and snapshots its asset/liability values via `check_pre_liquidation_condition_and_get_account_health` and `get_health_components` [1](#0-0) . Critically, neither `start_liquidation` nor `start_receivership` calls `bank.accrue_interest()` on any bank involved before performing this health/equity computation. By contrast, the "Classic" liquidation path, `lending_account_liquidate`, explicitly accrues interest on both the asset and liability banks immediately before computing pre-liquidation health [2](#0-1) .

### Finding Description
This mirrors the reported LiquidationSequencer bug class exactly: a code path that computes account health/liquidatability without first synchronizing accrued interest (the analog of `syncGlobalAccountingAndGracePeriod`), while a parallel, "correct" execution path (`LiquidationLibrary.batchLiquidateCdps` in the report; `lending_account_liquidate` here) does perform that synchronization first.

`start_receivership` uses `check_pre_liquidation_condition_and_get_account_health` [3](#0-2)  to decide if the account is unhealthy enough to be put into receivership, and separately snapshots `liq_record.cache.asset_value_maint` / `liability_value_maint` / `asset_value_equity` / `liability_value_equity` for later use [4](#0-3) . Because interest has not been accrued on the involved banks' `asset_share_value`/`liability_share_value` at this point, this snapshot reflects a stale accounting state that understates the true owed liability (interest accrual strictly increases outstanding liability value and decreases health) [5](#0-4) .

Within the same transaction, subsequent instructions inside the receivership window (`lending_account_withdraw`, `lending_account_repay`, and integrator withdraws) each call `bank.accrue_interest()` themselves before acting [6](#0-5) . This creates an inconsistency window: the health/equity values snapshotted at `start_liquidation` (pre-accrual) can diverge from the values used by any later step in the transaction (post-accrual), exactly analogous to the report's "TCR/Recover Mode/ICR could be outdated compared to what `CdpManager.batchLiquidateCdps()` later uses."

### Impact Explanation
Because interest accrual only reduces borrower health (it increases the liability amount recognized), understating it at `start_liquidation` time can cause `check_pre_liquidation_condition_and_get_account_health` to incorrectly treat an account as healthy (rejecting the liquidation with `HealthyAccount`) or to snapshot artificially high pre-liquidation health, when in fact the account is already underwater once interest is properly accrued. This can delay or block permissionless liquidation of genuinely unhealthy accounts, contributing to accumulation of bad debt for the protocol — a core insolvency-risk concern in the liquidation path. It also creates a snapshot/execution inconsistency for the `liq_record.cache` values captured in `start_receivership`, since interest accrues later in the same transaction via `withdraw`/`repay` calls, which is the same "stale-values-used-for-later-decisions" root cause flagged in the original report.

### Likelihood Explanation
This path is reachable by any unprivileged party: `start_liquidation` is explicitly documented as "(Permissionless)" [7](#0-6) , requiring no special role, just an unhealthy target account and a valid liquidation record. The discrepancy naturally arises whenever meaningful time has elapsed since the target bank's `last_update` timestamp (i.e., any bank with active borrow/lend activity that hasn't been touched recently), which is a routine, not theoretical, condition, given interest is normally only accrued "just before any balance change" [8](#0-7) .

### Recommendation
`start_receivership` (used by both `start_liquidation` and `start_deleverage`) should accrue interest on all banks referenced by the target account's balances before computing `check_pre_liquidation_condition_and_get_account_health` and the equity snapshot used for `liq_record.cache`, mirroring the `accrue_interest` calls already performed in `lending_account_liquidate`.

### Proof of Concept
I was not able to fully trace the downstream consumption of `liq_record.cache` values in `liquidate_end.rs` within the remaining investigation budget, so I cannot confirm with code-level certainty whether this snapshot/accrual mismatch is exploitable for direct fund extraction (e.g., a liquidator forcing extra withdrawal beyond what post-accrual health would allow) versus only causing blocked/delayed liquidations of legitimately unhealthy accounts. Both outcomes stem from the same root cause (missing interest sync before health evaluation in the receivership start path) and match the reported bug class, but the exact quantifiable impact path through `liquidate_end.rs` needs further verification, ideally with a Devin session that can read `liquidate_end.rs` and any tests exercising interest-accrual timing across `start_liquidation` → `withdraw`/`repay` → `end_liquidation`.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate_start.rs (L29-36)
```rust
/// (Permissionless) Begins a liquidation: snapshots the account and marks it in receivership. The
/// liquidator now has full control over the account until the end of the tx.
/// * Fails if account is healthy
/// * Fails if end liquidation instruction isn't at the end of this tx.
/// * Fails if the start liquidation instruction appears more than once in this tx.
/// * Fails if any mrgn instruction other than start, end, withdraw, or repay (or the equivalent
///   from a third party integration) are used within this tx.
pub fn start_liquidation<'info>(ctx: Context<'info, StartLiquidation<'info>>) -> MarginfiResult {
```

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate_start.rs (L85-128)
```rust
pub fn start_receivership<'info>(
    marginfi_account: &mut MarginfiAccount,
    liq_record: &mut LiquidationRecord,
    remaining_ais: &'info [AccountInfo<'info>],
    ignore_healthy: bool,
) -> MarginfiResult {
    // Note: the receiver can use the health cache state after this ix concludes to plan their
    // liquidation/deleverage strategy.
    let mut health_cache = HealthCache::zeroed();
    let mut liq_price_cache = LiquidationPriceCache::default();
    let (_pre_health, assets, liabs) = check_pre_liquidation_condition_and_get_account_health(
        marginfi_account,
        remaining_ais,
        None,
        &mut Some(&mut health_cache),
        HealthPriceMode::Live {
            liq_cache: Some(&mut liq_price_cache),
        },
        ignore_healthy,
    )?;

    // Use heap-efficient equity calculation
    let (assets_equity, liabs_equity) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Equity,
        &mut Some(&mut health_cache),
        HealthPriceMode::Live {
            liq_cache: Some(&mut liq_price_cache),
        },
    )?;

    write_liquidation_price_cache_from(marginfi_account, remaining_ais, &liq_price_cache)?;
    marginfi_account.health_cache = health_cache;
    marginfi_account.set_flag(ACCOUNT_IN_RECEIVERSHIP, false);
    marginfi_account.indexer_flags.has_ever_been_liquidated = 1;

    // Snapshot values to use in later checks
    liq_record.cache.asset_value_maint = assets.into();
    liq_record.cache.liability_value_maint = liabs.into();
    liq_record.cache.asset_value_equity = assets_equity.into();
    liq_record.cache.liability_value_equity = liabs_equity.into();

    Ok(())
```

**File:** programs/marginfi/src/instructions/marginfi_account/liquidate.rs (L153-185)
```rust
    let group = &*marginfi_group_loader.load()?;
    {
        ctx.accounts.asset_bank.load_mut()?.accrue_interest(
            current_timestamp,
            group,
            #[cfg(not(feature = "client"))]
            ctx.accounts.asset_bank.key(),
        )?;
        ctx.accounts.liab_bank.load_mut()?.accrue_interest(
            current_timestamp,
            group,
            #[cfg(not(feature = "client"))]
            ctx.accounts.liab_bank.key(),
        )?;
    }

    let init_liquidatee_remaining_len = liquidatee_accounts as usize;
    let liquidatee_accounts_starting_pos =
        ctx.remaining_accounts.len() - init_liquidatee_remaining_len;
    let liquidatee_remaining_accounts = &ctx.remaining_accounts[liquidatee_accounts_starting_pos..];

    liquidatee_marginfi_account.lending_account.sort_balances();

    let asset_bank_key = ctx.accounts.asset_bank.key();
    let liab_bank_key = ctx.accounts.liab_bank.key();
    let (pre_liquidation_health, _, _) = check_pre_liquidation_condition_and_get_account_health(
        &liquidatee_marginfi_account,
        liquidatee_remaining_accounts,
        Some(&liab_bank_key),
        &mut None,
        HealthPriceMode::Live { liq_cache: None },
        false,
    )?;
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L903-961)
```rust
pub fn check_pre_liquidation_condition_and_get_account_health<'info>(
    marginfi_account: &MarginfiAccount,
    remaining_ais: &'info [AccountInfo<'info>],
    liability_bank_pk: Option<&Pubkey>,
    health_cache: &mut Option<&mut HealthCache>,
    price_mode: HealthPriceMode<'_>,
    ignore_healthy: bool,
) -> MarginfiResult<(I80F48, I80F48, I80F48)> {
    check!(
        !marginfi_account.get_flag(ACCOUNT_IN_FLASHLOAN),
        MarginfiError::AccountInFlashloan
    );

    if let Some(bank_pk) = liability_bank_pk {
        let lending_account = &marginfi_account.lending_account;
        let liability_balance = lending_account
            .balances
            .iter()
            .find(|b| b.is_active() && b.bank_pk == *bank_pk)
            .ok_or(MarginfiError::LendingAccountBalanceNotFound)?;

        check!(
            !liability_balance.is_empty(BalanceSide::Liabilities),
            MarginfiError::NoLiabilitiesInLiabilityBank
        );

        check!(
            liability_balance.is_empty(BalanceSide::Assets),
            MarginfiError::AssetsInLiabilityBank
        );
    }

    // Get health components using heap reuse
    let (assets, liabs) = get_health_components(
        marginfi_account,
        remaining_ais,
        RequirementType::Maintenance,
        health_cache,
        price_mode,
    )?;

    let account_health = assets.checked_sub(liabs).ok_or_else(math_error!())?;
    let healthy = account_health > I80F48::ZERO;

    if let Some(cache) = health_cache.as_mut() {
        cache.set_healthy(healthy);
    }

    if healthy && !ignore_healthy {
        msg!(
            "pre_liquidation_health: {} ({} - {})",
            account_health,
            assets,
            liabs
        );
        return err!(MarginfiError::HealthyAccount);
    }

    Ok((account_health, assets, liabs))
```

**File:** programs/marginfi/src/state/bank.rs (L511-530)
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
```

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L96-103)
```rust
        };

        bank.accrue_interest(
            clock.unix_timestamp,
            &group,
            #[cfg(not(feature = "client"))]
            bank_loader.key(),
        )?;
```

**File:** README.md (L174-180)
```markdown
Realized rates on our banks are actually closer to the APY. Interest is computed just before any
balance change, so the rate at which different Banks compound actually varies. More popular Banks,
like SOL, compound every few minutes, or even every few seconds on more active days. Less popular
Banks might compound just a few times per week, but these Banks typically have very few borrows (and
thus a low APR to compound). Since interest compounds based on usage, the more popular our platform,
the more often interest compounds. Remember that interest accrues for all of a Bank's users at the
same time: that means if anyone transacts with a bank, the interest for *every* user compounds!
```
