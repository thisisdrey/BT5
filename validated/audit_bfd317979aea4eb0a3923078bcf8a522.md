### Title
Missing zero-address validation on `new_authority` in `transfer_to_new_account`/`transfer_to_new_account_pda` permanently locks migrated positions - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
Both `transfer_to_new_account` and `transfer_to_new_account_pda` accept an arbitrary `new_authority` account with no validation that it is non-zero (`Pubkey::default()`), mirroring the reported bug class of unchecked constructor/initializer addresses. The account is explicitly documented as unchecked: [1](#0-0) 

### Finding Description
`transfer_to_new_account` migrates a user's entire `MarginfiAccount` (all deposits, borrowed positions, emissions destination, flags) into a brand-new account whose `authority` field is set directly from the caller-supplied `new_authority` key, with zero validation: [2](#0-1) 

The instruction handler assigns this key straight into the new account and then irreversibly disables the old account: [3](#0-2) 

The `new_authority` account is declared as a completely unchecked account in the Anchor context: [1](#0-0) 

The PDA variant (`transfer_to_new_account_pda`) has the identical pattern and the identical unchecked comment: [4](#0-3) [5](#0-4) 

Notably, the off-chain CLI tooling is aware this is dangerous and defends against it client-side, confirming the on-chain instruction lacks the equivalent protection: [6](#0-5) 

If `new_authority` is `Pubkey::default()` (the zero address, which cannot correspond to any keypair capable of signing a transaction), the newly created account — now holding all migrated deposits/borrows — becomes permanently unreachable: no signer can ever satisfy `is_signer_authorized` against `Pubkey::default()`, and the old account has already been disabled (`ACCOUNT_DISABLED`, zeroed `lending_account`, `migrated_to` set) so the migration cannot be reversed or repeated.

### Impact Explanation
This causes a permanent lock/freeze of user funds: all collateral and outstanding positions held by the account are moved into a new account that no one can ever operate (deposit, withdraw, borrow, repay, or liquidate-trigger via authority-required paths), since no keypair signature can match `Pubkey::default()`. The source account is simultaneously and irreversibly disabled, so there is no path to recovery. This satisfies the "permanent lock/freeze" impact bar.

### Likelihood Explanation
The instruction is invoked directly by the account authority (an unprivileged user) as part of the normal account-migration flow, and `new_authority` is a plain, freely-chosen `UncheckedAccount` with no on-chain constraint. A single malformed/malicious client, buggy integration, front-end bug, or social-engineering attack that gets a user to submit `Pubkey::default()` (or any similarly unusable/non-signable address) as `new_authority` triggers total, irreversible loss of access to the funds. The existence of a client-side zero-address guard in the CLI (`p0-cli`) demonstrates the team recognizes this exact risk but did not enforce it at the program level, so it can be bypassed by any caller building the transaction directly against the on-chain program.

### Recommendation
Add an on-chain check in both `transfer_to_new_account` and `transfer_to_new_account_pda` rejecting `new_authority == Pubkey::default()` before initializing the new account, e.g.:
```rust
check!(
    ctx.accounts.new_authority.key() != Pubkey::default(),
    MarginfiError::InvalidAuthority // or a dedicated error
);
```
placed prior to `initialize_migrated_account` in `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`, so the check applies uniformly regardless of client-side protections.

### Proof of Concept
1. User calls `transfer_to_new_account` (or `transfer_to_new_account_pda`) on their own `MarginfiAccount`, supplying `new_authority = Pubkey::default()` and a valid new (uninitialized) `MarginfiAccount`/PDA.
2. `transfer_to_new_account` passes the fee-wallet check and flashloan/receivership/order checks (all unrelated to `new_authority`), then calls `initialize_migrated_account` which sets `new_account.authority = Pubkey::default()`: [7](#0-6) 
3. `finalize_migrated_old_account` disables the old account and zeroes its `lending_account`, moving all balance state into the new account: [8](#0-7) 
4. The transaction succeeds (no on-chain rejection of the zero address).
5. Any subsequent user-facing instruction requiring authority signature (deposit/withdraw/borrow/repay/close) on the new account calls `is_signer_authorized`, which can never be satisfied since no private key corresponds to `Pubkey::default()`; the funds/positions in the new account are permanently frozen, and the old account cannot be reused (`migrated_to` is set, and re-migration is blocked by the `AccountAlreadyMigrated` check).

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L23-37)
```rust
fn initialize_migrated_account(
    new_account: &mut MarginfiAccount,
    old_account: &MarginfiAccount,
    new_authority: Pubkey,
    current_timestamp: u64,
    old_account_key: Pubkey,
) {
    new_account.initialize(old_account.group, new_authority, current_timestamp);
    new_account.lending_account = old_account.lending_account;
    new_account.emissions_destination_account = old_account.emissions_destination_account;
    new_account.account_flags = old_account.account_flags;
    new_account.migrated_from = old_account_key;
    new_account.indexer_flags = old_account.indexer_flags;
    new_account.sync_indexer_flags();
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L39-49)
```rust
fn finalize_migrated_old_account(
    old_account: &mut MarginfiAccount,
    new_account_key: Pubkey,
    current_timestamp: u64,
) {
    old_account.migrated_to = new_account_key;
    old_account.last_update = current_timestamp;
    old_account.lending_account = LendingAccount::zeroed();
    old_account.set_flag(ACCOUNT_DISABLED, true);
    old_account.sync_indexer_flags();
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L91-105)
```rust
    let mut new_account = ctx.accounts.new_marginfi_account.load_init()?;
    let current_timestamp = Clock::get()?.unix_timestamp as u64;
    initialize_migrated_account(
        &mut new_account,
        &old_account,
        ctx.accounts.new_authority.key(),
        current_timestamp,
        ctx.accounts.old_marginfi_account.key(),
    );

    finalize_migrated_old_account(
        &mut old_account,
        ctx.accounts.new_marginfi_account.key(),
        current_timestamp,
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L158-159)
```rust
    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L182-241)
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

    let mut old_account = ctx.accounts.old_marginfi_account.load_mut()?;

    check!(
        !old_account.get_flag(ACCOUNT_IN_FLASHLOAN),
        MarginfiError::AccountInFlashloan
    );

    check!(
        !old_account.get_flag(ACCOUNT_IN_RECEIVERSHIP),
        MarginfiError::ForbiddenIx
    );

    check!(
        !old_account.get_flag(ACCOUNT_IN_ORDER_EXECUTION),
        MarginfiError::ForbiddenIx
    );

    check!(
        old_account.active_orders == 0,
        MarginfiError::IllegalAction,
        "Close all active orders before transfer"
    );

    // Prevent multiple migrations from the same account
    check_eq!(
        old_account.migrated_to,
        Pubkey::default(),
        MarginfiError::AccountAlreadyMigrated
    );

    // Validate third-party id restriction if provided
    if let Some(id) = third_party_id {
        if !is_allowed_cpi_for_third_party_id(&ctx.accounts.instructions_sysvar, id)? {
            return err!(MarginfiError::Unauthorized);
        }
    }

    let mut new_account = ctx.accounts.new_marginfi_account.load_init()?;
    let current_timestamp = Clock::get()?.unix_timestamp as u64;
    initialize_migrated_account(
        &mut new_account,
        &old_account,
        ctx.accounts.new_authority.key(),
        current_timestamp,
        ctx.accounts.old_marginfi_account.key(),
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L312-313)
```rust
    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** p0-cli/src/processor/account.rs (L1178-1181)
```rust
) -> Result<()> {
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```
