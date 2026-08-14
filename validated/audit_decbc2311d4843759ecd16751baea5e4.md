### Title
Missing zero-address validation for `new_authority` in `transfer_to_new_account`/`transfer_to_new_account_pda` allows permanent lock of migrated account - (File: programs/marginfi/src/instructions/marginfi_account/transfer_account.rs)

### Summary
The `TransferToNewAccount` / `TransferToNewAccountPda` instruction accepts an arbitrary `new_authority` account with no on-chain validation that it is not `Pubkey::default()` (Solana's analog of `address(0)`), mirroring the exact bug class in the external report: an update/transfer path that fails to reject the "burn address" equivalent, leaving downstream state (here, the migrated `MarginfiAccount`) permanently orphaned.

### Finding Description
`transfer_to_new_account` and `transfer_to_new_account_pda` initialize a brand-new `MarginfiAccount` whose `authority` field is set directly to `ctx.accounts.new_authority.key()`, with the account itself explicitly annotated as unchecked: [1](#0-0) [2](#0-1) 

The account struct comment even flags this as unchecked: `/// CHECK: WARN: New authority is completely unchecked`. [3](#0-2) 

Simultaneously, the old account is unconditionally finalized/disabled and marked as migrated, an irreversible action guarded only by a "no double migration" check: [4](#0-3) [5](#0-4) 

There is no on-chain check anywhere in this instruction rejecting `new_authority == Pubkey::default()`. The only place such a check exists is in the off-chain CLI helper, which does not protect the on-chain program from being invoked directly (e.g., by a custom client, contract, or an integrator building the instruction themselves): [6](#0-5) 

If a caller (accidentally, via a buggy integrator, or as a griefing action against their own irrecoverable account) submits `Pubkey::default()` as `new_authority`, the program will: (1) permanently disable/empty the old account (`ACCOUNT_DISABLED` flag, zeroed `lending_account`), and (2) create a brand-new `MarginfiAccount` whose `authority` is the system-owned default pubkey — an address that can never produce a valid signer for subsequent `is_signer_authorized` checks used by every other unprivileged instruction (deposit/withdraw/borrow/repay/liquidate/order flows), since no private key corresponds to `Pubkey::default()`.

### Impact Explanation
Any assets already moved to the new account (or deposited to it later by mistake) become permanently locked/frozen, as no signer can ever satisfy the `authority` check gating withdrawals, borrows, or further transfers on that `MarginfiAccount`. This is a genuine unrecoverable-funds/freeze scenario reachable through a normal, unprivileged, user-initiated instruction (`transfer_to_new_account` / `transfer_to_new_account_pda`), matching the "permanent lock/freeze" impact category.

### Likelihood Explanation
Likelihood is low-to-moderate: a legitimate user is unlikely to deliberately supply the zero pubkey, but the risk is realistic for third-party integrators (CPI callers, bots, or automated migration flows) that build the `TransferToNewAccount(Pda)` instruction programmatically without replicating the CLI's client-side guard. Because the check exists only off-chain in `p0-cli`, any other caller path (direct RPC calls, other SDKs, or malicious/careless third-party integrations using the whitelisted CPI mechanism) bypasses it entirely.

### Recommendation
Add an on-chain guard in both `transfer_to_new_account` and `transfer_to_new_account_pda` (in `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`) rejecting `ctx.accounts.new_authority.key() == Pubkey::default()` before finalizing the old account and initializing the new one, e.g.:

```rust
check!(
    ctx.accounts.new_authority.key() != Pubkey::default(),
    MarginfiError::InvalidAuthority // or a new dedicated error
);
```

This should be placed before `finalize_migrated_old_account` is called, so that the old account is never disabled if the new authority is invalid.

### Proof of Concept
1. Call `transfer_to_new_account` (or `transfer_to_new_account_pda`) with `new_authority = Pubkey::default()` and a valid `authority` signer for an existing, funded `old_marginfi_account`.
2. The instruction succeeds: `initialize_migrated_account` sets `new_account.authority = Pubkey::default()` [7](#0-6)  while `finalize_migrated_old_account` disables the old account and zeroes its `lending_account` [4](#0-3) .
3. Any subsequent attempt to withdraw/borrow/close balances from the new account fails permanently, since `is_signer_authorized` can never be satisfied by a signature for `Pubkey::default()`, leaving positions (if migrated) or later deposits inaccessible.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L155-166)
```rust
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

**File:** p0-cli/src/processor/account.rs (L1178-1181)
```rust
) -> Result<()> {
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```
