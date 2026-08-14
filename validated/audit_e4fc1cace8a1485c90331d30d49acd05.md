### Title
Single-Step, Unchecked Authority Transfer in `transfer_to_new_account`/`transfer_to_new_account_pda` Can Permanently Lock User Funds - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
The account-authority transfer functions `transfer_to_new_account` and `transfer_to_new_account_pda` allow any marginfi account authority to migrate their entire position (deposits and borrows) to a new account under an arbitrary `new_authority`, with no on-chain validation that `new_authority` is non-zero or otherwise valid, and no two-step "nominate + accept" confirmation. This mirrors the reported bug class ("No Transfer Ownership Pattern") applied to marginfi's account-migration flow instead of a generic `Ownable` contract.

### Finding Description
`TransferToNewAccount` and `TransferToNewAccountPda` accept `new_authority` as a completely unchecked account: [1](#0-0) [2](#0-1) 

In `transfer_to_new_account`, the new account is initialized directly with `ctx.accounts.new_authority.key()` and the old account is immediately disabled and zeroed out — there is no signature or acknowledgment required from `new_authority`, and no check that it isn't `Pubkey::default()` or some other unreachable key: [3](#0-2) [4](#0-3) 

The migration is irreversible: the old account is marked `ACCOUNT_DISABLED`, its `lending_account` is zeroed, and `migrated_to` is set, so it can never be reused, and a second migration attempt fails with `AccountAlreadyMigrated`: [5](#0-4) 

The only zero-address guard that exists is client-side, in the CLI helper `marginfi_account_transfer` — it is not enforced by the on-chain program, so any direct instruction call (or a different client) bypasses it entirely: [6](#0-5) 

The TS test suite explicitly acknowledges this is unrestricted user behavior ("WARN: User picks the new authority with no restrictions!"): [7](#0-6) 

### Impact Explanation
If a user (the account authority — an unprivileged, ordinary caller, not an admin) supplies a mistaken, mistyped, or zero `new_authority` (e.g., by client bug, copy-paste error, or malicious front-end), all of their deposited collateral and open positions are migrated to a `MarginfiAccount` controlled by an authority nobody can sign for. Because the old account is disabled and its `lending_account` zeroed as part of the same transaction, the funds become permanently locked/frozen with no recovery path — matching the "permanent lock/freeze" impact criterion. This is a direct on-chain accounting/state-change path (account migration, not admin-only), so it is in-scope per the unprivileged margin-account path restriction.

### Likelihood Explanation
Likelihood is moderate: this requires user error or a compromised/malicious front-end to select a bad `new_authority`, similar to the original report's scenario of accidentally transferring ownership to an invalid EOA. Given the instruction is entirely permissionless with respect to `new_authority` validity, and there is no analog to `acceptOwnership()`/two-step confirmation, any wrong destination key immediately and irreversibly locks the account's full contents.

### Recommendation
Add an on-chain zero-address (and ideally non-account-owner) check for `new_authority` in both `TransferToNewAccount` and `TransferToNewAccountPda` before initializing the new account, mirroring the client-side check that currently only exists in the CLI. Additionally, consider a two-step migration: have the old authority "propose" a migration destination, and require the `new_authority` to co-sign/accept the transfer (as already partially modeled by requiring the `new_marginfi_account` keypair to sign) before the old account is disabled and zeroed, so an invalid destination cannot silently and permanently strand funds.

### Proof of Concept
1. Attacker/careless user calls `transfer_to_new_account` (or `transfer_to_new_account_pda`) as the account `authority`, passing `new_authority = Pubkey::default()` (or any address they don't control) — see accounts struct in [8](#0-7) .
2. The instruction has no check rejecting a zero/invalid `new_authority`; it proceeds to call `initialize_migrated_account` with that key as the new account's `authority` [9](#0-8) .
3. In the same instruction, `finalize_migrated_old_account` disables the old account and zeroes its `lending_account`, transferring all balances conceptually to the new account [10](#0-9) [11](#0-10) .
4. `old_account.migrated_to` is now set, so `AccountAlreadyMigrated` blocks any retry/recovery [5](#0-4) , and the new account's authority (`Pubkey::default()` or an uncontrolled key) can never sign to withdraw/manage the funds, permanently freezing them.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L23-49)
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

**File:** tests/specs/basic/12_transfer_account.spec.ts (L55-56)
```typescript
  // Here the user moves authority to some new wallet. WARN: User picks the new authority with no
  // restrictions!
```
