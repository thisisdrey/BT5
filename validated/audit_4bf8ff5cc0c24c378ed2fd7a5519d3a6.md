The generic SECURITY.md does not explicitly exclude this issue. This confirms a valid, in-scope analog to the reported bug class.

### Title
Unchecked `new_authority` zero-address input permits permanent lock of migrated marginfi account funds - ([File: programs/marginfi/src/instructions/marginfi_account/transfer_account.rs])

### Summary
The `transfer_to_new_account` and `transfer_to_new_account_pda` instructions accept a `new_authority: UncheckedAccount<'info>` parameter that is explicitly annotated as `/// CHECK: WARN: New authority is completely unchecked` and never validated against `Pubkey::default()` (the zero address), analogous to the reported `StakingRewardsV3-1.sol` issue of unchecked input addresses.

### Finding Description
Both account-migration handlers copy the caller-supplied `new_authority` key directly into the freshly initialized account without any zero-address (or other sanity) check: [1](#0-0) [2](#0-1) 

The `new_authority` account is declared with no constraint at all in both `Accounts` structs: [3](#0-2) [4](#0-3) 

Once the migration completes, the old account is permanently disabled and its `lending_account` is zeroed out (balances moved to the new account), and this cannot be reversed because `migrated_to` is checked to prevent re-migration: [5](#0-4) [6](#0-5) 

If `new_authority` is `Pubkey::default()` (the zero/system-program address), the new account is created with an authority that no wallet can ever sign for, since `Signer<'info>` requires an actual matching keypair signature — the zero address has no corresponding private key. All position balances that were copied to `new_account.lending_account` therefore become permanently inaccessible: no withdrawal, repay, or liquidation-avoidance action can be signed for that account, and since the old account was already zeroed and flagged `ACCOUNT_DISABLED`, there is no recovery path.

Note that the CLI helper `marginfi_account_transfer` in `p0-cli` does guard against this off-chain: [7](#0-6)  — but this is only a client-side safeguard; the on-chain program itself performs no such check, so any direct instruction call (via a different client, a buggy integrator, or third-party front-end) bypasses it entirely.

### Impact Explanation
A user who is tricked, or whose integrating client has a bug/typo, into passing the zero address (or any other un-ownable/PDA-without-matching-authority address) as `new_authority` permanently loses all funds held in that marginfi account (deposits and any un-repaid borrow positions become orphaned in an unreachable account). This is a permanent freeze/loss of funds impact.

### Likelihood Explanation
Likelihood is low-to-moderate: the caller who invokes this instruction is the account's own current signer/authority, so this is not directly exploitable against another user's funds without a client bug, malicious front-end, or fat-finger error. However, because the on-chain program provides zero defense-in-depth (the check exists only in one specific CLI helper, not in the program itself), any third-party integrator, wallet, or alternative client calling the instruction directly is exposed to permanent, unrecoverable loss of user funds from a single bad input, with no on-chain safety net.

### Recommendation
Add an on-chain check in both `transfer_to_new_account` and `transfer_to_new_account_pda` that rejects `new_authority == Pubkey::default()` (and any other clearly invalid/system-owned sentinel address), e.g.:
```rust
check!(
    ctx.accounts.new_authority.key() != Pubkey::default(),
    MarginfiError::InvalidAuthority // or similar
);
```
This mirrors the fix recommended for the analogous `StakingRewardsV3-1.sol` finding — validating attacker/user-supplied addresses before they are persisted into critical state.

### Proof of Concept
1. User creates a marginfi account with active deposit/borrow balances.
2. User (or a buggy/malicious integrating client) calls `transfer_to_new_account_pda` (or `transfer_to_new_account`) with `new_authority = Pubkey::default()`.
3. The instruction succeeds: `initialize_migrated_account` sets `new_account.authority = Pubkey::default()` and copies all `lending_account` balances from the old account ( [8](#0-7) ); `finalize_migrated_old_account` zeroes the old account's `lending_account` and sets `ACCOUNT_DISABLED` ( [9](#0-8) ).
4. No wallet can ever produce a valid `Signer` for the zero-address authority on `new_marginfi_account`, so no subsequent withdraw/repay/close instruction (all of which require `is_signer_authorized` against `authority.key()`) can succeed — the funds are permanently locked.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L153-165)
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
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L307-317)
```rust
    pub authority: Signer<'info>,

    #[account(mut)]
    pub fee_payer: Signer<'info>,

    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,

    /// CHECK: Validated against group fee state cache
    #[account(mut)]
    pub global_fee_wallet: UncheckedAccount<'info>,
```

**File:** p0-cli/src/processor/account.rs (L1178-1181)
```rust
) -> Result<()> {
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```
