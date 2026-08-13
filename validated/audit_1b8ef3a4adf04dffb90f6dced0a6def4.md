Confirmed: `transfer_to_new_account` (and `transfer_to_new_account_pda`) accepts `new_authority: UncheckedAccount<'info>` marked explicitly `/// CHECK: WARN: New authority is completely unchecked` [1](#0-0)  and the handler writes it directly into the new account's `authority` field with no zero-address (or any other) validation [2](#0-1) , while simultaneously wiping and permanently disabling the old account [3](#0-2) .

### Title
Missing zero-address check on `new_authority` in `transfer_to_new_account`/`transfer_to_new_account_pda` permanently locks migrated user funds - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
`transfer_to_new_account` and `transfer_to_new_account_pda` let an account's own authority (unprivileged user action, no admin/group check) migrate all lending positions to a brand-new `MarginfiAccount` under an arbitrary `new_authority` pubkey. The `new_authority` account is declared `UncheckedAccount` with an explicit "WARN: completely unchecked" comment and is never validated against `Pubkey::default()` or any other unacceptable value before being set as the new account's `authority` and before the old account is disabled and zeroed out.

### Finding Description
`TransferToNewAccount`/`TransferToNewAccountPda` accept `new_authority` as a raw `UncheckedAccount` [1](#0-0) . In `transfer_to_new_account`, this key is passed straight to `initialize_migrated_account`, which sets `new_account.authority = new_authority` with no equality/zero check [4](#0-3) . Immediately afterward, the old account is disabled, its `lending_account` is zeroed (`LendingAccount::zeroed()`), and it is marked `migrated_to = new_account_key`, which also blocks any retry via the `AccountAlreadyMigrated` check on subsequent attempts [5](#0-4) . The same unchecked pattern exists in the PDA variant, which even derives the new account's PDA address itself from `new_authority` [6](#0-5) .

If `new_authority` is ever set to `Pubkey::default()` (the all-zero address, for which no private key exists) — whether by user error, a buggy front-end/integrator script, or a CPI caller passing an uninitialized/default value — the newly created account (holding all the migrated lending positions) becomes permanently un-signable: nobody can ever authorize `has_one = authority` checked instructions (withdraw, borrow, repay, deposit, close, etc.) against it. Because the old account is simultaneously disabled and its `lending_account` zeroed with `migrated_to` set, there is no path to reclaim the funds. This is the direct analog of the reported bug class ("Alice... accidentally sets ... to zero... resulting in the contract [account] becoming unusable"/"users' staked positions become permanently locked").

Note: The CLI helper `p0-cli/src/processor/account.rs::marginfi_account_transfer` does add a client-side zero-address guard, but this is exactly the kind of "reliance on front-end/script validation" the underlying bug class warns against — the on-chain program itself performs no such check, so any other caller (CPI, alternate client, raw transaction) is unprotected. [7](#0-6) 

### Impact Explanation
This results in a permanent freeze/lock of all lending positions (assets and liabilities) migrated into the new account — a direct, unauthorized-state-change/permanent-lock impact matching the required severity bar, since the resulting account can never be operated on again (no signer exists for the zero pubkey), and the old account's balances have already been zeroed and disabled by the same transaction.

### Likelihood Explanation
The instruction is permissionless with respect to the target value — it is only gated by the *old* account's authority signing, with zero on-chain constraint on `new_authority`. Any mistake in an integrating dApp, bot, CPI caller, or a malformed/default-initialized parameter (e.g., an uninitialized `Pubkey` in client code) triggers the issue in a single transaction, with no possibility of on-chain recovery.

### Recommendation
Add an explicit check in both `transfer_to_new_account` and `transfer_to_new_account_pda` that `ctx.accounts.new_authority.key() != Pubkey::default()` (and reject other clearly-invalid values, e.g. the program ID or a known system account) before initializing the new account and disabling the old one.

### Proof of Concept
1. User calls `transfer_to_new_account` (or `transfer_to_new_account_pda`) with `new_authority = Pubkey::default()` (e.g., due to an uninitialized variable in client code or a CPI caller passing a default pubkey).
2. The instruction succeeds: `new_account.authority` is set to `Pubkey::default()` [2](#0-1) , and `old_account` is disabled and zeroed in the same transaction [8](#0-7) .
3. All subsequent instructions requiring `has_one = authority` against the new account (`lending_account_withdraw`, `lending_account_borrow`, `lending_account_repay`, `marginfi_account_close`, etc.) can never be signed, since no keypair exists for the zero address.
4. The migrated funds are permanently locked with no recovery path.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L84-89)
```rust
    // Prevent multiple migrations from the same account
    check_eq!(
        old_account.migrated_to,
        Pubkey::default(),
        MarginfiError::AccountAlreadyMigrated
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L91-99)
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
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L101-105)
```rust
    finalize_migrated_old_account(
        &mut old_account,
        ctx.accounts.new_marginfi_account.key(),
        current_timestamp,
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L156-160)
```rust
    pub fee_payer: Signer<'info>,

    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,

```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L292-313)
```rust
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

**File:** p0-cli/src/processor/account.rs (L1179-1181)
```rust
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```
