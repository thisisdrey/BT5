Confirmed: the seed derivation for `MarginfiAccountInitializePda` (canonical creation path) and `TransferToNewAccountPda` are identical — both use `[MARGINFI_ACCOUNT_SEED, group, authority/new_authority, account_index, third_party_id]`. The `TransferToNewAccountPda` accounts struct places `new_authority` as a plain `UncheckedAccount` with the explicit comment "WARN: New authority is completely unchecked", and there is no signature or consent check tying `new_authority` to any approval.### Title
Permissionless PDA-squatting of arbitrary victim's canonical MarginfiAccount via unchecked `new_authority` in `transfer_to_new_account_pda` - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
`transfer_to_new_account_pda` derives the new `MarginfiAccount` PDA using `new_authority`, `account_index`, and `third_party_id` as seeds — the exact same seed scheme used by the canonical, self-service `marginfi_account_initialize_pda` instruction. `new_authority` is accepted as a completely unchecked, non-signing `UncheckedAccount`, so any attacker who owns any (even trivially funded) old `MarginfiAccount` can set `new_authority` to a victim's pubkey and permanently create/occupy the victim's default (`account_index=0`, `third_party_id=None`) PDA before the victim ever calls `marginfi_account_initialize_pda` themselves.

### Finding Description
In `transfer_to_new_account_pda` (`programs/marginfi/src/instructions/marginfi_account/transfer_account.rs:182-265`), the accounts struct `TransferToNewAccountPda` defines: [1](#0-0) 

- `new_marginfi_account` is `init`'d with seeds `[MARGINFI_ACCOUNT_SEED, group, new_authority.key(), account_index, third_party_id]` [2](#0-1) .
- `new_authority` is declared `pub new_authority: UncheckedAccount<'info>` with the comment `/// CHECK: WARN: New authority is completely unchecked` [3](#0-2) .

The only authorization checks in the instruction are on `old_marginfi_account` (must be owned/authorized by `authority`, not frozen, not already migrated, not in flashloan/receivership/order-execution) via `account_not_frozen_for_authority` and `is_signer_authorized` [4](#0-3) . There is no check binding `new_authority` to a signature, prior consent record, or any relationship with the caller.

Critically, this seed scheme is identical to the one used by the normal self-service account creation path `MarginfiAccountInitializePda` (`programs/marginfi/src/instructions/marginfi_account/initialize.rs:105-140`), which derives the PDA from `[MARGINFI_ACCOUNT_SEED, group, authority.key(), account_index, third_party_id]` and requires `authority` to sign [5](#0-4) . Because Anchor's `init` constraint fails with "account already in use" if the derived PDA already exists, whichever instruction creates the account for a given `(group, pubkey, account_index, third_party_id)` tuple first "wins" that PDA slot permanently.

Exploit flow:
1. Attacker creates their own `old_marginfi_account` (keypair-based, via `marginfi_account_initialize`) and deposits a trivial amount (or none, since the code has no minimum-balance requirement).
2. Attacker calls `transfer_to_new_account_pda(account_index=0, third_party_id=None)` with `new_authority = victim_pubkey`, `authority = attacker` (signer of `old_marginfi_account`), and pays the `ACCOUNT_TRANSFER_FEE`.
3. The instruction succeeds: it creates `new_marginfi_account` at the PDA `[MARGINFI_ACCOUNT_SEED, group, victim, 0, 0]`, sets `new_account.authority = victim_pubkey` (so the victim technically becomes the on-record authority) and migrates the attacker's old lending balances into it, and disables the old account (`ACCOUNT_DISABLED`, zeroed lending account) as seen in `finalize_migrated_old_account` [6](#0-5) .
4. When the victim later calls `marginfi_account_initialize_pda(account_index=0, third_party_id=None)` for themselves, the `init` constraint on the identical PDA seed fails ("account already in use"), permanently denying them their canonical default account address.

While the victim does end up as the nominal `authority` of the account that was created (so they aren't directly robbed of the migrated assets — they could, in principle, control the resulting account), the victim never consented to receiving this account, never signed the transaction, and is permanently prevented from using their own canonical default-index PDA for future integrations (e.g., CPI-based flows or off-chain systems that assume `derive_pda(group, authority, 0, None)` is the user's primary account and may not even know a foreign-migrated account with unexpected `migrated_from` history exists at that address). This breaks the invariant that only the rightful authority can create/claim their own canonical account identity permissionlessly.

### Impact Explanation
This is a permanent squatting/DoS of a specific deterministic address (index 0, no third-party tag) that many integrators are likely to treat as the default/canonical account for a given authority+group. A victim who has never interacted with marginfi could find their default PDA pre-occupied by an account with unexpected transfer history (`migrated_from` set to an attacker-controlled account), which:
- Forces account fragmentation (victim must use a different `account_index` going forward, breaking any tooling/CPI/integrator flow that assumes the deterministic default seed).
- Could confuse or complicate indexers/instrumentation/CPI integrations that key off the canonical `derive_pda` address, since the account will show `migrated_from` = an attacker-controlled account and non-zero indexer flags/history the victim never created.

This matches the "permanent lock/freeze of a resource" / unauthorized state change category in the bounty rules, scoped specifically to occupying the victim's canonical account slot without consent.

### Likelihood Explanation
- Fully permissionless and repeatable: the attacker needs only their own `old_marginfi_account` (created via the standard `marginfi_account_initialize`) with `migrated_to == Pubkey::default()`, no flashloan/receivership/order-execution flags set, and no minimum balance is required by the code.
- The attacker can target any victim pubkey, any `account_index`, and any allowed `third_party_id`, so front-running any specific victim before they create their canonical account is trivial and cheap (cost = `ACCOUNT_TRANSFER_FEE` + rent for the new PDA account, no oracle/health/liquidation constraints involved).
- No signer or explicit opt-in check exists for `new_authority`, so there is nothing stopping this today.

### Recommendation
Require the `new_authority` to explicitly consent to the transfer for the PDA-based path — e.g., require `new_authority` to be a `Signer` (co-signing the transfer transaction), or add a separate on-chain consent/allow-list mechanism (such as a pre-registered claim ticket signed by `new_authority`) before allowing `new_marginfi_account` to be initialized at their canonical PDA seed. At minimum, gate `transfer_to_new_account_pda` so it cannot target `account_index=0, third_party_id=None` (the "default" canonical slot) without the new authority's signature, since that is the slot most likely to be assumed as canonical by external integrators.

### Proof of Concept
Add an integration test in `programs/marginfi/tests/user_actions/transfer_account_pda.rs` alongside the existing `transfer_to_new_account_pda_success` test:

1. Create `attacker_old_account` via `MarginfiAccountInitialize` with `attacker` as authority; optionally deposit a trivial amount into a bank.
2. Generate a fresh `victim_keypair` that never signs or is otherwise involved in the transaction.
3. Derive `victim_pda = MarginfiAccount::derive_pda(group, victim_pubkey, 0, None, marginfi::ID)`.
4. Call `TransferToNewAccountPda` with `authority = attacker`, `new_authority = victim_pubkey` (no signature required from the victim), `new_marginfi_account = victim_pda`, `account_index = 0`, `third_party_id = None`. Assert the transaction succeeds (`res.is_ok()`), confirming the PDA is created without victim consent.
5. Then attempt `marginfi_account_initialize_pda` with `authority = victim_keypair` (the victim, now signing for real) for the same `(group, victim_pubkey, 0, None)` seed. Assert this second transaction fails with an "account already in use" style error, proving `NO_UNAUTHORIZED_ACCOUNT_CLAIM` is violated: the victim is permanently denied their canonical default PDA that they never created or consented to.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L277-290)
```rust
    #[account(
        mut,
        has_one = group @ MarginfiError::InvalidGroup,
        constraint = {
            let a = old_marginfi_account.load()?;
            account_not_frozen_for_authority(&a, authority.key())
        } @ MarginfiError::AccountFrozen,
        constraint = {
            let a = old_marginfi_account.load()?;
            let g = group.load()?;
            is_signer_authorized(&a, g.admin, authority.key(), false, false)
        } @ MarginfiError::Unauthorized
    )]
    pub old_marginfi_account: AccountLoader<'info, MarginfiAccount>,
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

**File:** programs/marginfi/src/instructions/marginfi_account/initialize.rs (L107-128)
```rust
pub struct MarginfiAccountInitializePda<'info> {
    #[account(
        constraint = !marginfi_group.load()?.is_protocol_paused() @ MarginfiError::ProtocolPaused
    )]
    pub marginfi_group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        init,
        payer = fee_payer,
        space = 8 + std::mem::size_of::<MarginfiAccount>(),
        seeds = [
            MARGINFI_ACCOUNT_SEED.as_bytes(),
            marginfi_group.key().as_ref(),
            authority.key().as_ref(),
            &account_index.to_le_bytes(),
            &third_party_id.unwrap_or(0).to_le_bytes(),
        ],
        bump
    )]
    pub marginfi_account: AccountLoader<'info, MarginfiAccount>,

    pub authority: Signer<'info>,
```
