### Title
Missing zero-address check on `new_authority` permanently locks migrated account funds - ([File: programs/marginfi/src/instructions/marginfi_account/transfer_account.rs])

### Summary
`TransferToNewAccount` / `TransferToNewAccountPda` let an account's authority migrate all of its lending positions to a freshly created `MarginfiAccount` and assign it a new authority supplied by the caller. The `new_authority` account is accepted as a completely unchecked `Pubkey` with no validation that it is non-zero, so a user can (accidentally or via a malicious front-end/relayer) set it to `Pubkey::default()`, permanently bricking control of the migrated account and all funds it holds.

### Finding Description
The `new_authority` field is declared as an `UncheckedAccount` with the explicit developer comment "WARN: New authority is completely unchecked": [1](#0-0) 

The same unchecked pattern exists in the PDA-account variant: [2](#0-1) 

`transfer_to_new_account_pda` writes this value directly into the new account's `authority` field via `initialize_migrated_account`, with no check that it is non-default: [3](#0-2) 

The old account is simultaneously disabled and marked as migrated to the new account, meaning it can no longer be operated by its original authority: [4](#0-3) 

Since `Pubkey::default()` (the all-zero pubkey) has no corresponding private key, no transaction can ever be signed as that authority. If `new_authority` is zero, the newly created account — which now holds the migrated lending positions/balances from the old account — becomes permanently uncontrollable, while the old account is disabled and cannot be used to recover.

Notably, the off-chain CLI helper for the same instruction explicitly guards against this exact case, confirming the team recognizes the danger but only enforces it client-side, not in the on-chain program: [5](#0-4) 

This is the direct analog of the reported bug class ("address variables not checked to be non-zero, causing loss of funds") applied to an unprivileged, user-reachable core account-management path.

### Impact Explanation
If `new_authority` is zero, all funds/positions moved into the new `MarginfiAccount` (deposits, collateral, and any borrow positions) become permanently inaccessible: nobody can sign transactions as the zero pubkey to withdraw, repay, or otherwise operate on the account, and the old account is simultaneously disabled (`ACCOUNT_DISABLED`) and marked migrated, closing off any fallback path. This satisfies the "permanent lock/freeze" impact class.

### Likelihood Explanation
The instruction is fully permissionless from the account authority's perspective — no admin or privileged role is required, and the check is entirely absent on-chain (the `UncheckedAccount` doc comment explicitly says so). It is exploitable via a compromised/malicious client, a coding bug in a third-party integrator's transaction builder, or a supply-chain-compromised front-end, without needing the account owner's intent, since the value is fully attacker/tooling-controlled and unchecked by the program.

### Recommendation
Add an explicit constraint in `TransferToNewAccount`/`TransferToNewAccountPda` rejecting `new_authority == Pubkey::default()`, e.g.:
```rust
#[account(constraint = new_authority.key() != Pubkey::default() @ MarginfiError::InvalidAuthority)]
pub new_authority: UncheckedAccount<'info>,
```
mirroring the check already present in the off-chain CLI helper.

### Proof of Concept
1. User (or a malicious/buggy relayer acting on the user's behalf) calls `transfer_to_new_account` (or `transfer_to_new_account_pda`) passing `new_authority = Pubkey::default()`.
2. The instruction succeeds: `old_account` is disabled and `migrated_to` set to the new account; `new_account.authority` is set to `Pubkey::default()` via `initialize_migrated_account`.
3. All lending positions previously held by the old account are now associated with `new_account`, whose authority is the zero pubkey.
4. No wallet can ever sign as `Pubkey::default()`, so `new_account` can never call `lending_account_withdraw`, `lending_account_repay`, etc. — the funds are permanently locked, and the old account cannot be reactivated because it is `ACCOUNT_DISABLED` with `migrated_to` set.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L158-159)
```rust
    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L233-244)
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
    new_account.account_index = account_index;
    new_account.third_party_index = third_party_id.unwrap_or(0);
    new_account.bump = ctx.bumps.new_marginfi_account;
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L246-250)
```rust
    finalize_migrated_old_account(
        &mut old_account,
        ctx.accounts.new_marginfi_account.key(),
        current_timestamp,
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
