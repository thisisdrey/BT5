### Title
No Two-Step Confirmation for MarginfiAccount Authority Transfer, Enabling Permanent Loss of Account Access - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
The `TransferToNewAccount` and `TransferToNewAccountPda` instructions let a `MarginfiAccount` authority migrate all their lending positions to a new account under a `new_authority` pubkey in a single atomic call, with no confirmation step from the receiving address. The `new_authority` account is explicitly documented as unchecked, and the old account is immediately disabled and its state zeroed out.

### Finding Description
In `transfer_to_new_account`, the `new_authority` field is declared as: [1](#0-0) 

The comment itself states `/// CHECK: WARN: New authority is completely unchecked`. This value is taken directly from user-supplied input and immediately used to initialize the new account's `authority` field in `initialize_migrated_account`, called from within `transfer_to_new_account`: [2](#0-1) 

Simultaneously, `finalize_migrated_old_account` immediately marks the old account as disabled and wipes its `lending_account`, setting `migrated_to` to the new account: [3](#0-2) 

There is a subsequent explicit check preventing re-migration of an already-migrated account (`AccountAlreadyMigrated`): [4](#0-3) 

This means the operation is a one-shot, irrevocable transfer of authority over all deposited/borrowed positions to whatever pubkey the caller supplies as `new_authority`, with no mechanism for the new authority to "accept" the transfer and no ability to reverse or retry once submitted — mirroring exactly the single-step ownership-transfer bug class described in the report (owner enters wrong address, and access is permanently lost), except here it applies to an unprivileged user's own margin account rather than to a privileged admin role. The on-chain client code itself only defends against the literal zero pubkey, and only client-side in the CLI helper, not in the on-chain program: [5](#0-4) 

That zero-address check exists only in the off-chain CLI tooling (`marginfi_account_transfer`), not in the Anchor instruction handler itself, so any other typo'd-but-valid pubkey (the far more common real-world mistake) is entirely unguarded on-chain.

### Impact Explanation
If a user makes any mistake specifying `new_authority` — a typo, wrong keypair, wrong derived PDA, or a pubkey they do not control — their entire position (all deposits, collateral, and open borrows tracked by that `MarginfiAccount`) becomes permanently controlled by that unintended address. The old account is disabled and its `lending_account` zeroed in the very same transaction, and re-migration is blocked by the `AccountAlreadyMigrated` check, so there is no recovery path. This is a permanent loss of access to the user's own funds/collateral, matching the "permanent lock/freeze" impact category.

### Likelihood Explanation
This code path is reachable by any unprivileged user at any time via `transfer_to_new_account` / `transfer_to_new_account_pda`, both of which are ordinary user-facing instructions (tested via `try_transfer_account` and the TS integration specs `tests/specs/basic/12_transfer_account.spec.ts` / `12a_transfer_account_pda.spec.ts`). No admin or validator privilege is required, and it requires only a normal fat-fingering error, which is a realistic and common class of user mistake, especially since these flows accept a bare pubkey with no ENS-like verification or two-step acceptance.

### Recommendation
- **Short term**: Implement a two-step confirm flow for `TransferToNewAccount`/`TransferToNewAccountPda`: the current authority proposes `new_authority` and the account is put in a "pending transfer" state; the transfer is only finalized after a subsequent transaction signed by `new_authority` (or the new account's authority) accepts it. Only after acceptance should the old account be disabled and drained of `lending_account` state.
- **Long term**: Audit all instructions that irrevocably move authority/ownership of user or admin state in a single transaction (e.g. `MarginfiGroupConfigure`'s admin fields) and document, for each, whether recovery is possible if the wrong address is supplied, adding two-step confirmation wherever the mistake would be irrecoverable.

### Proof of Concept
1. User 0 creates a `MarginfiAccount` and deposits/borrows normally (as in `programs/marginfi/tests/admin_actions/account_transfer.rs`, lines 12-49).
2. User 0 calls `transfer_to_new_account` supplying a `new_authority` pubkey that is mistyped or otherwise not controlled by them (no on-chain check prevents this beyond it not being `Pubkey::default()` in CLI tooling, which itself doesn't run on-chain).
3. The transaction succeeds: the old account is immediately flagged `ACCOUNT_DISABLED`, `migrated_to` is set, and `lending_account` is zeroed (`finalize_migrated_old_account`), while the new account's `authority` is set to the unintended pubkey (`initialize_migrated_account`).
4. User 0 attempts to migrate again to correct the mistake — this fails with `AccountAlreadyMigrated`: [4](#0-3) 
5. User 0 has now permanently lost the ability to control the position; only the (unintended) `new_authority` key can act on the new account.

### Citations

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L158-159)
```rust
    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** p0-cli/src/processor/account.rs (L1179-1181)
```rust
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```
