### Title
On-chain program allows account migration to the zero-address authority, permanently freezing all migrated funds - ([File: programs/marginfi/src/instructions/marginfi_account/transfer_account.rs])

### Summary
`transfer_to_new_account` and `transfer_to_new_account_pda` let a user migrate all lending positions from an existing `MarginfiAccount` into a brand-new account under a caller-supplied `new_authority`, with the old account permanently disabled. The on-chain instruction places **no validation** on `new_authority`, explicitly marking it `/// CHECK: WARN: New authority is completely unchecked`. The zero-pubkey check that exists in this codebase is only present in the off-chain CLI helper (`p0-cli`), not enforced by the program itself. If `new_authority == Pubkey::default()` is passed directly to the program (bypassing the CLI, e.g., via a raw instruction, another wallet, or an integrator/dApp), the new account's `authority` field is permanently set to `Pubkey::default()` while all the old account's lending positions are moved into it and the old account is disabled and can never be reused for that migration path — mirroring the PoolTogether `TwabController` issue where resetting a delegation to `address(0)` sends the delegated balance to an address nobody can control.

### Finding Description
`transfer_to_new_account` copies the caller's positions into a new account and immediately finalizes the migration: [1](#0-0) 

`initialize_migrated_account` sets `new_account.authority = new_authority` directly with no zero-check: [2](#0-1) 

The `TransferToNewAccount` accounts struct defines `new_authority` as a completely unchecked account: [3](#0-2) 

The same pattern exists in the PDA variant, `transfer_to_new_account_pda`: [4](#0-3) [5](#0-4) 

Once migration finalizes, the old account is disabled and recorded as migrated, closing off any path back: [6](#0-5) [7](#0-6) 

The only place in the codebase that guards against a zero `new_authority` is the CLI client helper, which is not part of the on-chain program and is trivially bypassed by anyone constructing the instruction directly (e.g., via an SDK, integrator, or third-party frontend): [8](#0-7) 

Every downstream operation on a marginfi account (deposits, withdrawals, borrows, transfers) is gated by `is_signer_authorized`, which compares the transaction signer's key against `MarginfiAccount.authority` (or the group admin for privileged flows). Because `Pubkey::default()` has no corresponding private key, no signer can ever satisfy this check for the new account, and the new account's `active_orders`/flags don't stop a permissioned admin from acting either — admin paths are limited to freeze/close-bank type actions, not asset withdrawal on a user's behalf. This is directly analogous to the reported PoolTogether bug: default value (there, delegate `address(0)` mapping to self; here, `authority = default()`) is reachable via a normal user-facing operation, and once reached, the resulting balance/authority is unrecoverable because the write path that would normally let you set it back (a fresh account init or another transfer) can't reproduce ownership of `Pubkey::default()`.

### Impact Explanation
Setting `new_authority` to `Pubkey::default()` freezes all of the account's collateral and liabilities forever: no signer can ever authorize withdrawals, borrows, repayments, or another `transfer_to_new_account` call on the new account, since `is_signer_authorized` requires the signer to match `authority`, and nobody controls the zero pubkey's private key. This constitutes a permanent lock/freeze of user funds — the exact impact class validated for the analog (High severity in the original report), triggered here by an unprivileged user (or anyone who convinces a user's wallet/integrator to sign with a bad `new_authority`) via a normal, non-admin instruction.

### Likelihood Explanation
The check is only present client-side in `p0-cli`, so any integrator, alternate frontend, malicious dApp, or hand-crafted transaction that calls `transfer_to_new_account`/`transfer_to_new_account_pda` with `new_authority = Pubkey::default()` (accidentally via an uninitialized/default Pubkey variable, or via a malicious counterparty in a third-party integration) can trigger it. This is a self-inflicted or integrator-error class scenario rather than one requiring an adversary to attack another user's account (the `authority` signer must consent to the transfer), but it is easily triggered by benign bugs in third-party client code (e.g., an uninitialized `Pubkey` defaults to all-zero in many Solana client libraries), making it a realistic and moderately likely occurrence given no on-chain safety net exists.

### Recommendation
Add an on-chain check in both `transfer_to_new_account` and `transfer_to_new_account_pda` (in `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`) that rejects `new_authority == Pubkey::default()` before initializing the new account, e.g.:
```rust
check!(
    ctx.accounts.new_authority.key() != Pubkey::default(),
    MarginfiError::IllegalAction,
    "Cannot transfer to the zero-address authority"
);
```
This should be enforced identically to how `p0-cli` already guards this client-side, but must live in the program itself since client-side checks are not a security boundary.

### Proof of Concept
1. User (or an integrator's frontend) calls `transfer_to_new_account` directly (bypassing `p0-cli`) with `new_authority = Pubkey::default()`, targeting a fresh `new_marginfi_account` keypair, and passes their `authority` signature.
2. The instruction runs `initialize_migrated_account(..., new_authority = Pubkey::default(), ...)`, copying `old_account.lending_account` (all balances/positions) into `new_account`, and setting `new_account.authority = Pubkey::default()`. [9](#0-8) 
3. `finalize_migrated_old_account` disables the old account and marks `migrated_to = new_marginfi_account`, preventing re-migration: [10](#0-9) 
4. Any subsequent attempt to operate on the new account (withdraw, borrow, or call `transfer_to_new_account` again to "fix" the authority) fails `is_signer_authorized`'s check against `authority = Pubkey::default()`, because no wallet can sign as the zero pubkey — the funds in `new_account.lending_account` are permanently locked.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L153-160)
```rust
    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L307-314)
```rust
    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

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
