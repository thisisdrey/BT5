### Title
Permissionless `InitLiquidationRecord` allows attaching a liquidation record to any healthy account, forcing victims to submit an extra `close_liquidation_record` transaction before closing their account - ([File: programs/marginfi/src/instructions/marginfi_account/init_liquid_record.rs])

### Summary
`initialize_liquidation_record` never checks that the target `marginfi_account` is unhealthy, in receivership, or otherwise a legitimate liquidation candidate, and its `Accounts` struct places no `has_one = authority` or signer constraint tying the caller to the account owner. Any unprivileged caller can therefore create a `LiquidationRecord` PDA for an arbitrary victim account, which sets `marginfi_account.liquidation_record` to a non-default value and is later enforced as a precondition for `close_account`.

### Finding Description
`InitLiquidationRecord::marginfi_account` is only constrained as `#[account(mut)]` with no `has_one = authority` and no health/state check [1](#0-0) . The instruction unconditionally writes `liq_record.marginfi_account = ctx.accounts.marginfi_account.key()` and `marginfi_account.liquidation_record = ctx.accounts.liquidation_record.key()` regardless of the account's health, debt, or receivership status [2](#0-1) . The only implicit precondition is that the PDA (seeded by `LIQUIDATION_RECORD_SEED` + the victim account key) doesn't already exist, via Anchor's `init` constraint — there is no explicit check that `liquidation_record == default` beforehand, and no requirement that the caller controls the account.

Once set, `close_account` requires `marginfi_account.liquidation_record == Pubkey::default()` before the account can be closed [3](#0-2) , so any account hit by this griefing must first call `close_liquidation_record`.

However, `close_liquidation_record` is itself permissionless and, critically, allows immediate closure when the record was never used: it computes `last_activity` as the max entry timestamp, and only enforces the 60-day inactivity window `if last_activity > 0` [4](#0-3) . A freshly initialized record has `entries = [LiquidationEntry::default(); 4]` with `timestamp = 0` for all entries [5](#0-4) [6](#0-5) , so `last_activity == 0` and the record can be closed immediately by anyone, with rent refunded to the original `record_payer` (the attacker) rather than the victim [7](#0-6) .

### Impact Explanation
This confirms the described griefing vector: an attacker can spray `InitLiquidationRecord` calls across arbitrary victim `marginfi_account` pubkeys, forcing each account owner to submit one extra `close_liquidation_record` instruction before they can call `close_account`. This is a real, unauthorized state mutation on third-party accounts caused by an unprivileged caller, and at scale it is a low-cost, mass-griefing vector against every account owner attempting to close their account. It does not, however, cause fund loss, insolvency, or a permanent lock — the record can always be closed immediately (no 60-day wait) since a never-liquidated record has all-zero timestamps, and rent is returned to the attacker who paid for it, not stranded from the victim. The impact is scoped strictly to forced extra transactions/griefing, not asset loss or permanent freeze.

### Likelihood Explanation
Highly feasible and repeatable: the instruction has no signer/authority tie to the victim account and no health precondition, so it can be called by any funded wallet against any `marginfi_account` pubkey in a simple loop, limited only by rent cost (recoverable by the attacker later) and transaction throughput.

### Recommendation
Add a health/eligibility precondition to `initialize_liquidation_record` (e.g., require the account to already be flagged `ACCOUNT_IN_RECEIVERSHIP`/unhealthy, or require the caller to be the account's `authority`, or require the risk engine to confirm the account is liquidatable) before allowing a `LiquidationRecord` to be attached, so that permissionless record creation cannot be used to encumber arbitrary healthy accounts.

### Proof of Concept
Rust integration test plan:
1. Create N synthetic healthy/zero-debt `MarginfiAccount`s (no risky positions).
2. As an attacker wallet (distinct from each account's `authority`), call `initialize_liquidation_record` for each account's PDA in a loop.
3. Assert that after the loop, `marginfi_account.liquidation_record != Pubkey::default()` for all N accounts (state mutation succeeded despite health being fine).
4. Assert that calling `close_account` for any of these accounts fails with `IllegalAction` ("Close liquidation record before closing account").
5. Assert that calling `close_liquidation_record` (permissionless, by anyone) immediately succeeds (no 60-day wait, since `entries` timestamps are all zero) and rent returns to `record_payer` (attacker), then `close_account` succeeds.
6. Assert `NO_STRANDED_FUNDS`: rent that was stranded briefly belongs to the attacker who paid it, not the victim, and no victim funds are ever at risk — confirming the impact is limited to a mandatory extra transaction for victims, not fund loss.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/init_liquid_record.rs (L11-26)
```rust
pub fn initialize_liquidation_record(ctx: Context<InitLiquidationRecord>) -> MarginfiResult {
    let mut marginfi_account = ctx.accounts.marginfi_account.load_mut()?;
    let mut liq_record = ctx.accounts.liquidation_record.load_init()?;

    liq_record.key = ctx.accounts.liquidation_record.key();
    liq_record.record_payer = ctx.accounts.fee_payer.key();
    liq_record.marginfi_account = ctx.accounts.marginfi_account.key();
    liq_record.entries = [LiquidationEntry::default(); 4];
    liq_record.cache = LiquidationCache::default();

    // Link the record back to the MarginfiAccount. This also serves to inform liquidators if the
    // record exists without performing a fetch. If this field is non-default, it exists.
    marginfi_account.liquidation_record = ctx.accounts.liquidation_record.key();

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/init_liquid_record.rs (L28-46)
```rust
#[derive(Accounts)]
pub struct InitLiquidationRecord<'info> {
    #[account(mut)]
    pub marginfi_account: AccountLoader<'info, MarginfiAccount>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    #[account(
        init,
        payer = fee_payer,
        seeds = [LIQUIDATION_RECORD_SEED.as_bytes(), marginfi_account.key().as_ref()],
        bump,
        space = 8 + std::mem::size_of::<LiquidationRecord>()
    )]
    pub liquidation_record: AccountLoader<'info, LiquidationRecord>,

    pub system_program: Program<'info, System>,
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/close.rs (L13-17)
```rust
    check!(
        marginfi_account.liquidation_record == Pubkey::default(),
        MarginfiError::IllegalAction,
        "Close liquidation record before closing account"
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/close_liquid_record.rs (L29-48)
```rust
pub fn close_liquidation_record(ctx: Context<CloseLiquidationRecord>) -> MarginfiResult {
    let record = ctx.accounts.liquidation_record.load()?;

    let last_activity = record
        .entries
        .iter()
        .map(|e| e.timestamp)
        .max()
        .unwrap_or(0);

    // Records that were never used (all timestamps zero) can be closed immediately.
    // Otherwise, require 60 days of inactivity.
    if last_activity > 0 {
        let now = Clock::get()?.unix_timestamp;
        check!(
            now.saturating_sub(last_activity) >= INACTIVITY_PERIOD_SECS,
            MarginfiError::IllegalAction,
            "Liquidation record must be inactive for at least 60 days"
        );
    }
```

**File:** programs/marginfi/src/instructions/marginfi_account/close_liquid_record.rs (L71-93)
```rust
    #[account(
        mut,
        close = record_payer,
        has_one = marginfi_account @ MarginfiError::InvalidLiquidationRecord,
        constraint = {
            let record = liquidation_record.load()?;
            record.liquidation_receiver == Pubkey::default()
        } @ MarginfiError::IllegalAction
    )]
    pub liquidation_record: AccountLoader<'info, LiquidationRecord>,

    /// The wallet that originally paid to create this record.
    /// Rent is returned here via Anchor's `close` constraint.
    /// CHECK: validated by the liquidation_record's record_payer field
    #[account(
        mut,
        constraint = {
            let record = liquidation_record.load()?;
            record.record_payer == record_payer.key()
        } @ MarginfiError::Unauthorized
    )]
    pub record_payer: UncheckedAccount<'info>,
}
```

**File:** type-crate/src/types/liquidation_record.rs (L58-68)
```rust
pub struct LiquidationEntry {
    /// Dollar amount seized
    /// * An f64 stored as bytes
    pub asset_amount_seized: [u8; 8],
    /// Dollar amount repaid
    /// * An f64 stored as bytes
    pub liab_amount_repaid: [u8; 8],
    pub placeholder0: u64,
    pub timestamp: i64,
    _reserved0: [u8; 16],
}
```
