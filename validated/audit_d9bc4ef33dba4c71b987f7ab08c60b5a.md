### Title
Single-step, unchecked `new_authority` in `transfer_to_new_account`/`transfer_to_new_account_pda` can permanently lock user funds - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
`transfer_to_new_account` (and its PDA variant) lets a marginfi account authority migrate all lending positions to a brand-new `MarginfiAccount` under a caller-supplied `new_authority` pubkey in a single atomic, one-step transaction, with no verification that the given `new_authority` is controllable by anyone. This mirrors the reported bug class (single-step critical-address change with no validation of correctness), but the affected role here is the unprivileged account authority rather than a protocol admin.

### Finding Description
The `new_authority` account is explicitly documented as unchecked: [1](#0-0) . The instruction only requires that the caller sign as the current `authority` and that the old account is not already migrated, frozen, in a flashloan, in receivership, or has active orders [2](#0-1) . It then initializes the new account with `new_authority` as its owner and disables the old account permanently by setting `ACCOUNT_DISABLED`, zeroing `lending_account`, and recording `migrated_to` [3](#0-2) [4](#0-3) . Once migrated, retrying or reversing the migration is explicitly blocked via the `AccountAlreadyMigrated` check on the old account [5](#0-4) , and this is confirmed by tests asserting a second transfer attempt fails [6](#0-5) . There is no admin- or protocol-level recovery path to reassign the `authority` of the new account or to "undo" the migration once positions have moved. The client-side helper (`p0-cli`) only guards against the zero-address case, not against an arbitrary incorrect-but-nonzero address [7](#0-6) , and the TypeScript test suite explicitly documents this as "WARN: User picks the new authority with no restrictions!" [8](#0-7) .

### Impact Explanation
If a user (or an integrator automating this flow) supplies an incorrect, mistyped, or otherwise uncontrolled `new_authority` pubkey, all of that account's collateral and borrow positions are migrated into a new `MarginfiAccount` that nobody can sign for. Because the old account is disabled and the migration is single-use and irreversible, the funds become permanently locked/frozen with no recovery mechanism — a direct "permanent lock/freeze" outcome as defined in scope. This is analogous to the reported `uberOwner` issue where an unchecked, single-step address change with no validation locks critical functionality, except the blast radius here is scoped to the affected user's own position value rather than the whole protocol.

### Likelihood Explanation
Low-to-moderate probability, high impact — matching the judge's characterization of the original finding. Likelihood is nontrivial because this is a normal user-facing instruction (used for account migration/rotation, including third-party/PDA integrations) rather than a rare admin-only action, so fat-fingered addresses, copy-paste errors, or integration bugs computing `new_authority` (e.g., PDA derivation mismatches for `transfer_to_new_account_pda`) are plausible failure modes. The one mitigating factor is that users generally control this input themselves and can be diligent, but there is no on-chain safety net at all (no confirmation/claim step, no ability to reverse) once the transaction lands.

### Recommendation
Convert `transfer_to_new_account` / `transfer_to_new_account_pda` into a two-step process: first, mark the new account/authority as "pending" without disabling the old account or moving balances; second, require a transaction signed by `new_authority` to claim/confirm the new account before the migration finalizes and the old account is disabled. Alternatively, keep the old account recoverable (e.g., not immediately zeroing balances or setting `ACCOUNT_DISABLED`) until the new authority has proven signing capability by executing at least one transaction from the new account.

### Proof of Concept
1. User calls `transfer_to_new_account` with a `new_authority` pubkey for which no private key exists (typo, wrong derivation, or address copied from an unrelated context).
2. The instruction succeeds: `initialize_migrated_account` sets `new_account.authority = new_authority` and moves all lending balances there [9](#0-8) ; `finalize_migrated_old_account` disables the old account and zeroes its `lending_account` [3](#0-2) .
3. The user attempts to retry the transfer from the old account to fix the mistake; it fails with `AccountAlreadyMigrated` [5](#0-4)  as confirmed by the existing regression test [6](#0-5) .
4. No instruction exists to reassign `authority` on the new account or restore the old one, so all deposited/borrowed positions are permanently inaccessible.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L63-89)
```rust
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

**File:** programs/marginfi/tests/admin_actions/account_transfer.rs (L98-110)
```rust
    // Attempting to transfer again should fail
    let new_account_again = Keypair::new();
    let res = marginfi_account
        .try_transfer_account(
            new_account_again.pubkey(),
            new_authority.pubkey(),
            Some(clone_keypair(&authority)),
            None,
            &new_account_again,
            test_f.marginfi_group.fee_wallet,
        )
        .await;
    assert!(res.is_err());
```

**File:** p0-cli/src/processor/account.rs (L1174-1181)
```rust
pub fn marginfi_account_transfer(
    profile: &Profile,
    config: &Config,
    new_authority: Pubkey,
) -> Result<()> {
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```

**File:** tests/specs/basic/12_transfer_account.spec.ts (L55-56)
```typescript
  // Here the user moves authority to some new wallet. WARN: User picks the new authority with no
  // restrictions!
```
