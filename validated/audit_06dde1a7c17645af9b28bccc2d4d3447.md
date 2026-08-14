### Title
Missing validation of `new_authority` in account migration allows permanent lock of user funds - ([File: programs/marginfi/src/instructions/marginfi_account/transfer_account.rs])

### Summary
`transfer_to_new_account` and `transfer_to_new_account_pda` copy a `MarginfiAccount`'s entire `lending_account` (all deposits/borrows) into a freshly initialized account whose `authority` field is set directly from the caller-supplied `new_authority` account, with zero validation of its value. The account is explicitly flagged in code as unchecked: `/// CHECK: WARN: New authority is completely unchecked` [1](#0-0) . There is no check that `new_authority != Pubkey::default()` (or any other unusable/unowned key) before it is written into the new account's authority field and the old account is permanently disabled.

### Finding Description
`transfer_to_new_account` loads the old account, verifies fee/flag/migration-state invariants, then calls `initialize_migrated_account`, which sets `new_account.authority = new_authority` directly from the raw `UncheckedAccount` key with no sanity check: [2](#0-1) . Immediately after, `finalize_migrated_old_account` copies the `lending_account` (deposits/borrows) into the new account, zeroes the old account's `lending_account`, and permanently sets `ACCOUNT_DISABLED` on the old account: [3](#0-2) . The `new_authority` account is declared as a completely unchecked `UncheckedAccount` in both `TransferToNewAccount` and `TransferToNewAccountPda`: [4](#0-3) [5](#0-4) .

If a caller passes `Pubkey::default()` (or any key with no known private key / not owned by a valid signer path) as `new_authority`, the new account is created with `authority = Pubkey::default()`. Since `Signer<'info>` for the new account's future operations must match this authority, nobody can ever sign as this authority, and the account (holding the migrated `lending_account` balances) becomes permanently inaccessible — deposits can never be withdrawn and borrows can never be repaid or managed through the account owner path.

This is the direct analog of the reported bug class: an address parameter (`new_authority`/`_wrapped`/`_underlying`/`_controller` in the PieDao report) is used to initialize critical state without a zero-value check, permanently disabling functionality tied to that address.

### Impact Explanation
Successful triggering permanently locks the entirety of a marginfi account's deposit/borrow position (the migrated `lending_account`) with no recovery path, since the resulting account's authority is an unusable key. This matches the "permanent lock/freeze" impact category.

### Likelihood Explanation
The instruction is only reachable by the account's own current `authority`, gated by `is_signer_authorized` and `account_not_frozen_for_authority` constraints requiring the caller to already control the source account: [6](#0-5) . This means triggering the bug against oneself requires only a client-side mistake (e.g., a bad copy-paste, malformed integration passing a zero/burn key, or a compromised/buggy frontend) rather than any privileged or third-party attacker action — there is no path for one user to force this outcome onto another user's account without that user's own signature. The complete absence of a zero-check, despite the code author explicitly calling out the field as "completely unchecked," combined with a real, unrecoverable fund-loss outcome for the affected account, still represents a genuine missing-input-validation defect worth fixing, even though the practical likelihood is bounded by requiring self-signed execution.

### Recommendation
Add an explicit check that `new_authority.key() != Pubkey::default()` (and ideally, that it is a plausible authority, e.g. not a program-owned/non-signable account) before initializing the migrated account in both `transfer_to_new_account` and `transfer_to_new_account_pda`, mirroring the recommendation in the referenced report to validate address parameters before they are used to initialize critical state.

### Proof of Concept
1. User calls `transfer_to_new_account` (or `transfer_to_new_account_pda`) with `old_marginfi_account` set to their existing account containing deposits/borrows, and `new_authority` set to `Pubkey::default()`.
2. `initialize_migrated_account` sets `new_account.authority = Pubkey::default()` and copies over `lending_account` from the old account [7](#0-6) .
3. `finalize_migrated_old_account` disables the old account and zeroes its `lending_account`, moving all balances into the new account [8](#0-7) .
4. The new account's authority (`Pubkey::default()`) has no corresponding private key, so no future instruction requiring `authority: Signer<'info>` matching this account can ever be signed — the account's deposits/borrows are permanently frozen.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L131-144)
```rust
    #[account(
        mut,
        has_one = group @ MarginfiError::InvalidGroup,
        constraint = {
            let a = old_marginfi_account.load()?;
            account_not_frozen_for_authority(&a, authority.key())
        } @ MarginfiError::AccountFrozen,
        constraint = {
            let a = old_marginfi_account.load()?;
            let g = group.load()?;
            is_signer_authorized(&a, g.admin, authority.key(), false, false)
        } @ MarginfiError::Unauthorized
    )]
    pub old_marginfi_account: AccountLoader<'info, MarginfiAccount>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L146-159)
```rust
    #[account(
        init,
        payer = fee_payer,
        space = 8 + std::mem::size_of::<MarginfiAccount>()
    )]
    pub new_marginfi_account: AccountLoader<'info, MarginfiAccount>,

    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L290-313)
```rust
    pub old_marginfi_account: AccountLoader<'info, MarginfiAccount>,

    #[account(
        init,
        payer = fee_payer,
        space = 8 + std::mem::size_of::<MarginfiAccount>(),
        seeds = [
            MARGINFI_ACCOUNT_SEED.as_bytes(),
            group.key().as_ref(),
            new_authority.key().as_ref(),
            &account_index.to_le_bytes(),
            &third_party_id.unwrap_or(0).to_le_bytes(),
        ],
        bump
    )]
    pub new_marginfi_account: AccountLoader<'info, MarginfiAccount>,

    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```
