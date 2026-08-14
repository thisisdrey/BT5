### Title
Lack of two-step authority transfer in `transfer_to_new_account`/`transfer_to_new_account_pda` permanently locks a marginfi account's funds - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
`transfer_to_new_account` and `transfer_to_new_account_pda` let any marginfi account authority migrate all lending positions to a brand-new `MarginfiAccount` under an arbitrary `new_authority` in a single, non-reversible instruction. The new authority is accepted with zero validation and there is no claim/acceptance step, matching the "lack of two-step role transfer" bug class from the external report.

### Finding Description
Both instructions accept `new_authority` as a completely unchecked account: [1](#0-0) 

The instruction immediately performs the migration: it initializes the new account with `new_authority` as owner, moves all `lending_account` balances into it, and disables/empties the old account, recording it as migrated so the operation can never be retried: [2](#0-1) 

The `AccountAlreadyMigrated` check makes the old account permanently unusable once `migrated_to` is set, and there is no mechanism for the designated `new_authority` to "claim" or "accept" the transfer before it takes effect — the state change is final at the moment the current authority signs: [3](#0-2) 

This is invoked by the CPI-friendly `transfer_to_new_account`/`transfer_to_new_account_pda` handlers exposed in `lib.rs`, callable by any account authority (unprivileged path, no admin gating): [4](#0-3) 

Tests explicitly acknowledge the design accepts an unrestricted new authority with no verification step ("WARN: User picks the new authority with no restrictions!"): [5](#0-4) 

Because there is no two-step "propose then claim" flow (unlike, e.g., typical `pending_admin`/`accept_admin` patterns), a mistyped `new_authority`, an address without an accessible private key, or (in the PDA variant) a `new_authority` whose PDA cannot practically be signed for, results in funds being irreversibly transferred to an address that can never sign transactions to operate the new account — with the old account already disabled and its balances zeroed, and re-migration permanently blocked by `AccountAlreadyMigrated`.

### Impact Explanation
This causes a permanent lock/freeze of the user's own deposited/borrowed position value: once migrated, the old account is disabled and stripped of balances, and the new account is only usable by whoever controls `new_authority`. If that key is inaccessible (typo, wrong keypair, wrong PDA derivation, or an address contributed by third-party integration tooling), the position becomes permanently unrecoverable, with no admin/self-recovery path in the instruction. This mirrors the reported class where a single-step, unchecked-recipient role/authority change can brick access to protocol value, except here it directly locks user collateral/borrow positions rather than fee routing.

### Likelihood Explanation
Likelihood is non-trivial: the instruction is fully permissionless for the account's own authority, requires no additional confirmation, and is also exposed to third-party CPI integrators (`is_allowed_cpi_for_third_party_id`) and PDA-seed-driven flows where a wrong `account_index`/`third_party_id`/`new_authority` combination silently produces an unrecoverable PDA. Given it's a normal user-facing account-management operation (not a rare admin action), routine user/integrator error is a realistic and repeatable trigger.

### Recommendation
Implement a two-step transfer for `transfer_to_new_account`/`transfer_to_new_account_pda`: first record the proposed `new_authority` (and/or pre-create/initialize the new account in a "pending" state), then require a subsequent transaction signed by `new_authority` to finalize/claim the migration before the old account is disabled and its balances zeroed. Alternatively, require the `new_authority` to co-sign the initial transfer instruction so accessibility of the destination key is verified before any state changes to the old account occur.

### Proof of Concept
1. User A creates a `MarginfiAccount` and deposits/borrows normally.
2. User A calls `transfer_to_new_account` (or `transfer_to_new_account_pda`), supplying a `new_authority` pubkey that is mistyped or otherwise has no accessible private key (see accounts struct at `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs:146-159`).
3. The instruction succeeds unconditionally (no validation on `new_authority` beyond being a valid pubkey): the new account is initialized with all of the old account's `lending_account` balances (`programs/marginfi/src/instructions/marginfi_account/transfer_account.rs:91-99`), and the old account is disabled and zeroed with `migrated_to` set (`programs/marginfi/src/instructions/marginfi_account/transfer_account.rs:39-49,101-105`).
4. Any further attempt to migrate the old account fails with `AccountAlreadyMigrated` (`programs/marginfi/src/instructions/marginfi_account/transfer_account.rs:85-89`), and since nobody can sign as `new_authority`, the new account (and all migrated funds/positions) is permanently inaccessible.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L84-105)
```rust
    // Prevent multiple migrations from the same account
    check_eq!(
        old_account.migrated_to,
        Pubkey::default(),
        MarginfiError::AccountAlreadyMigrated
    );

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L153-159)
```rust
    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** programs/marginfi/src/lib.rs (L496-516)
```rust
    /// (account authority) Transfer all positions to a new account under a new authority. The old
    /// account is disabled. Pays a flat SOL fee to the protocol.
    pub fn transfer_to_new_account(ctx: Context<TransferToNewAccount>) -> MarginfiResult {
        marginfi_account::transfer_to_new_account(ctx)
    }

    /// (account authority) Same as `transfer_to_new_account` except the resulting account is a PDA
    ///
    /// seeds:
    /// - marginfi_group
    /// - authority: The account authority (owner)  
    /// - account_index: A u16 value to allow multiple accounts per authority
    /// - third_party_id: Optional u16 for third-party tagging. Seeds < PDA_FREE_THRESHOLD can be
    ///   used freely. For a dedicated seed used by just your program (via CPI), contact us.
    pub fn transfer_to_new_account_pda(
        ctx: Context<TransferToNewAccountPda>,
        account_index: u16,
        third_party_id: Option<u16>,
    ) -> MarginfiResult {
        marginfi_account::transfer_to_new_account_pda(ctx, account_index, third_party_id)
    }
```

**File:** tests/specs/basic/12_transfer_account.spec.ts (L55-56)
```typescript
  // Here the user moves authority to some new wallet. WARN: User picks the new authority with no
  // restrictions!
```
