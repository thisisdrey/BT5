### Title
Stale cached `global_fee_wallet` in `MarginfiGroup.fee_state_cache` causes protocol fees/rent to be sent to an outdated fee wallet - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`, `programs/marginfi/src/instructions/marginfi_account/admin_close.rs`, `programs/marginfi/src/instructions/marginfi_group/propagate_fee_state.rs`)

### Summary
`MarginfiGroup` stores a cached copy of the program-wide fee wallet in `fee_state_cache.global_fee_wallet` [1](#0-0) . This cache is only refreshed when someone explicitly calls the permissionless `propagate_fee` instruction [2](#0-1) . Several unprivileged/permissionless user-facing instructions (`transfer_to_new_account`, `transfer_to_new_account_pda`, `admin_close_account`) validate the `global_fee_wallet` account against this cached value — not against the live `FeeState.global_fee_wallet` — and either transfer SOL fees or close-account rent to it [3](#0-2) [4](#0-3) . This is the same bug class as the reported `SofamonWearable.royaltyFeeTo` issue: a fee-receiver address is cached on a secondary account instead of being read live, and if the global admin updates `FeeState.global_fee_wallet` via `edit_global_fee_state` [5](#0-4)  without every group calling `propagate_fee` afterward, protocol fees keep flowing to the old wallet.

### Finding Description
`FeeState` is the single global PDA holding the canonical `global_fee_wallet`, editable only by `global_fee_admin` via `edit_global_fee_state` [6](#0-5) . Each `MarginfiGroup` keeps its own copy of this wallet in `fee_state_cache.global_fee_wallet`, populated at group `initialize` time [7](#0-6)  and refreshed only by the separate, permissionless `propagate_fee` instruction [2](#0-1) .

Unlike `lending_pool_add_bank`/`lending_pool_collect_bank_fees`, which correctly re-derive/validate against the live `FeeState.global_fee_wallet` on every call (`has_one = global_fee_wallet` on the `fee_state` account, or reading `ctx.accounts.fee_state.load()?.global_fee_wallet` directly) [8](#0-7) [9](#0-8) , the account-transfer/close paths trust the group's cached copy instead:

- `transfer_to_new_account` and `transfer_to_new_account_pda` check the passed `global_fee_wallet` account against `group.fee_state_cache.global_fee_wallet` and then transfer `ACCOUNT_TRANSFER_FEE` lamports to it [10](#0-9) .
- `admin_close_account` closes an inactive `MarginfiAccount` and sends its rent lamports to `global_fee_wallet`, validated only against `group.fee_state_cache.global_fee_wallet` [11](#0-10) .

If `global_fee_admin` rotates the fee wallet (e.g., migrating custody, compromised-key rotation, treasury restructuring) via `edit_global_fee_state`, the change is immediately live in `FeeState`, but every `MarginfiGroup` that has not since had `propagate_fee` called against it will continue routing account-transfer fees and closed-account rent to the stale, previously-configured wallet — silently, for an indefinite period, since `propagate_fee` is not automatically invoked on every fee-mutating action and there is no guarantee any actor calls it promptly per group.

### Impact Explanation
Protocol fee revenue (SOL transfer fees and closed-account rent) is misdirected away from the currently-designated fee wallet to an old one, for an indeterminate amount of time and across an indeterminate number of groups, until someone happens to call `propagate_fee` for each affected group. This is analogous to case (3) in the reference report ("`protocolFeeTo` is updated ... royalties sent to the old address"). Because both the old and new wallets are admin-controlled addresses (not user funds and not zero-address in the typical case, since the cache is initialized at group creation), the immediate loss is of protocol-fee routing correctness rather than user fund theft; nonetheless it is an unauthorized/incorrect state effect that diverts protocol revenue and can persist across many groups if `propagate_fee` isn't proactively run after every fee-wallet rotation.

### Likelihood Explanation
Low-to-moderate. It requires (a) `global_fee_admin` rotating `fee_wallet` and (b) at least one `MarginfiGroup` not having `propagate_fee` re-run before further `transfer_to_new_account`/`admin_close_account` calls occur. Since fee-wallet rotations are rare admin operations and `propagate_fee` is permissionless (anyone can trigger it), an attentive admin/keeper would typically re-sync promptly, but there is no on-chain enforcement guaranteeing this happens for every group before the next fee-relevant transaction.

### Recommendation
For instructions that move real funds/rent to the "global fee wallet" (`transfer_to_new_account`, `transfer_to_new_account_pda`, `admin_close_account`), validate against the live `FeeState.global_fee_wallet` (as `lending_pool_add_bank`/`lending_pool_collect_bank_fees` already do) instead of the group's `fee_state_cache.global_fee_wallet`. If the cache must be kept for gas/compute reasons, ensure `propagate_fee` is enforced/refreshed as part of, or immediately before, any instruction that disburses funds based on it, or add a staleness check (e.g., reject if `fee_state_cache.last_update` is older than some bound) so stale caches cannot silently misroute fees indefinitely.

### Proof of Concept
1. `global_fee_admin` calls `edit_global_fee_state` with a new `fee_wallet` value (`programs/marginfi/src/instructions/marginfi_group/edit_global_fee.rs:32-39`), updating `FeeState.global_fee_wallet` on-chain immediately.
2. For any `MarginfiGroup` that has not yet had `propagate_fee` invoked since this change, `group.fee_state_cache.global_fee_wallet` still equals the old wallet.
3. A user calls `transfer_to_new_account_pda` for that group. The instruction checks `ctx.accounts.global_fee_wallet.key() == group.fee_state_cache.global_fee_wallet` (old wallet) and transfers `ACCOUNT_TRANSFER_FEE` lamports to that old wallet (`programs/marginfi/src/instructions/marginfi_account/transfer_account.rs:188-194`), even though the canonical `FeeState.global_fee_wallet` is now different.
4. Similarly, `admin_close_account` can close an eligible inactive account and send its rent lamports to the same stale wallet (`programs/marginfi/src/instructions/marginfi_account/admin_close.rs:58-71`).
5. This can repeat for every group that lags on calling `propagate_fee`, diverting protocol fee revenue to the deprecated wallet until each group's cache is refreshed.

### Citations

**File:** type-crate/src/types/group.rs (L102-112)
```rust
/// Cached fee configuration propagated from the global FeeState
pub struct FeeStateCache {
    /// The wallet that receives program-level fees
    pub global_fee_wallet: Pubkey,
    /// Fixed fee APR charged to borrowers (program-level)
    pub program_fee_fixed: WrappedI80F48,
    /// Proportional fee rate on interest (program-level)
    pub program_fee_rate: WrappedI80F48,
    /// Unix timestamp of the last fee state propagation
    pub last_update: i64,
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/propagate_fee_state.rs (L21-34)
```rust
pub fn propagate_fee(ctx: Context<PropagateFee>) -> Result<()> {
    let mut group = ctx.accounts.marginfi_group.load_mut()?;
    let fee_state = ctx.accounts.fee_state.load()?;

    group.fee_state_cache.global_fee_wallet = fee_state.global_fee_wallet;
    group.fee_state_cache.program_fee_fixed = fee_state.program_fee_fixed;
    group.fee_state_cache.program_fee_rate = fee_state.program_fee_rate;

    let clock = Clock::get()?;
    group.fee_state_cache.last_update = clock.unix_timestamp;

    group
        .panic_state_cache
        .update_from_panic_state(&fee_state.panic_state, clock.unix_timestamp);
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L51-59)
```rust
pub fn transfer_to_new_account(ctx: Context<TransferToNewAccount>) -> MarginfiResult {
    // Validate the global fee wallet and claim a nominal fee
    let group = ctx.accounts.group.load()?;
    check_eq!(
        ctx.accounts.global_fee_wallet.key(),
        group.fee_state_cache.global_fee_wallet,
        MarginfiError::InvalidFeeAta
    );
    anchor_lang::system_program::transfer(ctx.accounts.transfer_fee(), ACCOUNT_TRANSFER_FEE)?;
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L182-194)
```rust
pub fn transfer_to_new_account_pda(
    ctx: Context<TransferToNewAccountPda>,
    account_index: u16,
    third_party_id: Option<u16>,
) -> MarginfiResult {
    // Validate the global fee wallet and claim a nominal fee
    let group = ctx.accounts.group.load()?;
    check_eq!(
        ctx.accounts.global_fee_wallet.key(),
        group.fee_state_cache.global_fee_wallet,
        MarginfiError::InvalidFeeAta
    );
    anchor_lang::system_program::transfer(ctx.accounts.transfer_fee(), ACCOUNT_TRANSFER_FEE)?;
```

**File:** programs/marginfi/src/instructions/marginfi_account/admin_close.rs (L54-72)
```rust
#[derive(Accounts)]
pub struct AdminCloseAccount<'info> {
    pub group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        mut,
        has_one = group @ MarginfiError::InvalidGroup,
        close = global_fee_wallet
    )]
    pub marginfi_account: AccountLoader<'info, MarginfiAccount>,

    /// CHECK: Validated against group fee state cache
    #[account(
        mut,
        constraint = global_fee_wallet.key() == group.load()?.fee_state_cache.global_fee_wallet
            @ MarginfiError::InvalidGlobalFeeWallet
    )]
    pub global_fee_wallet: UncheckedAccount<'info>,
}
```

**File:** programs/marginfi/src/instructions/marginfi_group/edit_global_fee.rs (L10-39)
```rust
pub fn edit_fee_state(
    ctx: Context<EditFeeState>,
    admin: Option<Pubkey>,
    fee_wallet: Option<Pubkey>,
    bank_init_flat_sol_fee: Option<u32>,
    liquidation_flat_sol_fee: Option<u32>,
    order_init_flat_sol_fee: Option<u32>,
    program_fee_fixed: Option<WrappedI80F48>,
    program_fee_rate: Option<WrappedI80F48>,
    liquidation_max_fee: Option<WrappedI80F48>,
    order_execution_max_fee: Option<WrappedI80F48>,
    pause_delegate_admin: Option<Pubkey>,
) -> Result<()> {
    let mut fee_state = ctx.accounts.fee_state.load_mut()?;
    if let Some(admin) = admin {
        msg!(
            "Updating global_fee_admin: {:?} -> {:?}",
            fee_state.global_fee_admin,
            admin
        );
        fee_state.global_fee_admin = admin;
    }
    if let Some(fee_wallet) = fee_wallet {
        msg!(
            "Updating global_fee_wallet: {:?} -> {:?}",
            fee_state.global_fee_wallet,
            fee_wallet
        );
        fee_state.global_fee_wallet = fee_wallet;
    }
```

**File:** programs/marginfi/src/instructions/marginfi_group/initialize.rs (L21-32)
```rust
    let fee_state = ctx.accounts.fee_state.load()?;

    // The fuzzer should ignore this because the "Clock" mock sysvar doesn't load until after the
    // group is init. Eventually we might fix the fuzzer to load the clock first...
    #[cfg(not(feature = "client"))]
    {
        let clock = Clock::get()?;
        marginfi_group.fee_state_cache.last_update = clock.unix_timestamp;
    }
    marginfi_group.fee_state_cache.global_fee_wallet = fee_state.global_fee_wallet;
    marginfi_group.fee_state_cache.program_fee_fixed = fee_state.program_fee_fixed;
    marginfi_group.fee_state_cache.program_fee_rate = fee_state.program_fee_rate;
```

**File:** programs/marginfi/src/instructions/marginfi_group/add_pool.rs (L112-126)
```rust
    /// Pays to init accounts and pays `fee_state.bank_init_flat_sol_fee` lamports to the protocol
    #[account(mut)]
    pub fee_payer: Signer<'info>,

    // Note: there is just one FeeState per program, so no further check is required.
    #[account(
        seeds = [FEE_STATE_SEED.as_bytes()],
        bump,
        has_one = global_fee_wallet @ MarginfiError::InvalidFeeWallet
    )]
    pub fee_state: AccountLoader<'info, FeeState>,

    /// CHECK: The fee admin's native SOL wallet, validated against fee state
    #[account(mut)]
    pub global_fee_wallet: UncheckedAccount<'info>,
```

**File:** programs/marginfi/src/instructions/marginfi_group/collect_bank_fees.rs (L26-38)
```rust
    // Validate the program fee ata is correct
    {
        let mint = &bank.mint;
        let global_fee_wallet = &ctx.accounts.fee_state.load()?.global_fee_wallet;
        let token_program_id = &ctx.accounts.token_program.key();
        let program_fee_ata = &ctx.accounts.fee_ata.key();
        let ata_expected =
            get_associated_token_address_with_program_id(global_fee_wallet, mint, token_program_id);
        check!(
            program_fee_ata.eq(&ata_expected),
            MarginfiError::InvalidFeeAta
        );
    }
```
