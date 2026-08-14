### Title
`transfer_to_new_account`/`transfer_to_new_account_pda` accept an unchecked, unvalidated `new_authority` including the zero/default `Pubkey`, permanently locking the migrated account and disabling the old one - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
The `TransferToNewAccount` and `TransferToNewAccountPda` instructions take `new_authority` as a completely unvalidated `UncheckedAccount`, explicitly documented as "WARN: New authority is completely unchecked." [1](#0-0)  This is directly analogous to the reported bug class: a critical address parameter (here, the account authority, analogous to `merchant` in the report) is never checked against `Pubkey::default()`/`0x0` before being permanently written into on-chain state.

### Finding Description
`transfer_to_new_account` calls `initialize_migrated_account(&mut new_account, &old_account, ctx.accounts.new_authority.key(), ...)`, which sets the new account's `authority` field directly to whatever `new_authority` value is supplied by the caller, with no sanity check. [2](#0-1)  The account struct declares `new_authority` as an `UncheckedAccount` with no `require_keys_neq` or zero-address constraint. [3](#0-2) 

At the same time, the old account is unconditionally and irreversibly disabled: `finalize_migrated_old_account` sets `migrated_to`, zeroes the `lending_account`, and sets the `ACCOUNT_DISABLED` flag. [4](#0-3)  A subsequent check (`check_eq!(old_account.migrated_to, Pubkey::default(), ...)`) means migration can never be repeated once performed, so this is a one-way operation. [5](#0-4) 

If `new_authority` is `Pubkey::default()` (the zero/system-program address), the newly created account (which now holds all balances/positions from the old account) will have an `authority` field that can never be a signer of a real transaction — no private key exists for the default `Pubkey`. This permanently locks/freezes all funds and positions moved into the new account, while the source account is already disabled and cannot be un-migrated. The same unchecked pattern exists in the PDA variant, `TransferToNewAccountPda`. [6](#0-5) 

This mirrors the reported bug class exactly: an address parameter accepted at "construction"/initialization time with no `!= 0x0` sanity check, resulting in a contract/account instance that can never be operated again.

### Impact Explanation
Any authenticated marginfi account owner (the account authority, an unprivileged user) who calls `marginfi_account_transfer`/`transfer_to_new_account_pda` and supplies (or is tricked/misled by client tooling into supplying, or fat-fingers) `new_authority = Pubkey::default()` will cause:
- Permanent loss of access/freeze of all assets and open positions transferred to the new account (no one can ever sign as `Pubkey::default()`).
- The old account is simultaneously and irrevocably disabled (`ACCOUNT_DISABLED`, `migrated_to` set), with no path to reverse the migration.

This satisfies the "permanent lock/freeze" impact category from the validation rules. Note that the CLI (`p0-cli`) explicitly guards against this exact case with a client-side check `if new_authority == Pubkey::default() { bail!(...) }`, [7](#0-6)  which confirms that the underlying on-chain program itself performs no such check — client-side guards are not a substitute for an on-chain invariant, since the instruction is directly callable by any signer bypassing the CLI.

### Likelihood Explanation
The instruction is permissionless from the account authority's perspective (only requires the authority's signature and no group/admin gating beyond `is_signer_authorized`), and the vulnerable field is entirely attacker/user-controlled with zero on-chain validation. The only barrier is that the transaction must be crafted directly (bypassing the CLI's client-side check) — this is trivial for any third-party front-end, SDK, or malformed transaction, and could also occur accidentally through a bug in any calling code that fails to set `new_authority` before submitting.

### Recommendation
Add an on-chain constraint requiring `new_authority.key() != Pubkey::default()` (and ideally that `new_authority` is not the marginfi program ID or other known-unusable addresses) in both `TransferToNewAccount` and `TransferToNewAccountPda` account validation structs, mirroring the check already present client-side in `p0-cli`. This should be a `require_keys_neq!` / `check!` executed before `initialize_migrated_account` is called and before the old account is disabled, so that the entire transaction fails atomically rather than silently locking funds.

### Proof of Concept
1. Create a marginfi account and deposit funds into it (`marginfi_account_initialize` + `lending_account_deposit`).
2. Call `marginfi_account_transfer` (`TransferToNewAccount`) as the account's authority, supplying `new_authority = Pubkey::default()` (system program / all-zero pubkey) and a fresh `new_marginfi_account` keypair.
3. Observe: the transaction succeeds; `new_account.authority == Pubkey::default()`; `old_account.migrated_to == new_account_pubkey` and `old_account.account_flags` has `ACCOUNT_DISABLED` set.
4. Attempt to sign any future instruction (deposit/withdraw/borrow/close) using `new_marginfi_account` as authority `Pubkey::default()` — this is cryptographically impossible, since no keypair exists for the default `Pubkey`. All funds in `new_marginfi_account` are now permanently unreachable, and the old account cannot be migrated again due to the `AccountAlreadyMigrated` check.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L153-166)
```rust
    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,

    /// CHECK: Validated against group fee state cache
    #[account(mut)]
    pub global_fee_wallet: UncheckedAccount<'info>,

    pub system_program: Program<'info, System>,
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L310-318)
```rust
    pub fee_payer: Signer<'info>,

    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,

    /// CHECK: Validated against group fee state cache
    #[account(mut)]
    pub global_fee_wallet: UncheckedAccount<'info>,

```

**File:** p0-cli/src/processor/account.rs (L1179-1181)
```rust
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```
