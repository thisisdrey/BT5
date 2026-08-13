### Title
Unvalidated `new_authority` in `transfer_to_new_account`/`transfer_to_new_account_pda` allows permanent lock of all account funds - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
The Golom finding describes rewards becoming permanently unretrievable once an NFT's owner is set to `address(0)`, because the reward-transfer function has no fallback for a burned/zero-owner token and OZ's ERC20 reverts on transfers to `address(0)`. The closest reachable analog in marginfi-v2 is the on-chain `TransferToNewAccount`/`TransferToNewAccountPda` instructions, where the new account's `authority` field is populated directly from an **entirely unchecked** `new_authority` account, with no validation against `Pubkey::default()` or any other unusable/un-signable key.

### Finding Description
`transfer_to_new_account` and `transfer_to_new_account_pda` migrate all lending positions from `old_marginfi_account` to a freshly initialized `new_marginfi_account`, whose `authority` is set to `ctx.accounts.new_authority.key()`: [1](#0-0) 

The `new_authority` account is declared as a completely unchecked account: [2](#0-1) 

There is no on-chain constraint preventing `new_authority` from being `Pubkey::default()` (or any other public key with no known private key). Every subsequent instruction that operates on a `MarginfiAccount` (deposit, withdraw, borrow, repay, close, transfer again, etc.) requires a `Signer` matching the account's `authority` field (enforced via `has_one = authority` or `is_signer_authorized`), so once `authority` is set to an address nobody can sign for, the account—and all of the lending positions/collateral migrated into it—becomes permanently inaccessible to any unprivileged actor. This mirrors the Golom bug class: a state transition that sets an "owner" field to an unusable/zero value, after which normal transfer/claim paths can never succeed again for that value, permanently locking associated value.

Notably, the off-chain `p0-cli` client explicitly guards against this exact scenario: [3](#0-2) 

This confirms the marginfi team is aware that a zero/default `new_authority` is dangerous, but the on-chain program itself performs no equivalent check — the CLI-level `bail!` is not a substitute for validation inside `transfer_to_new_account`/`transfer_to_new_account_pda`, since the instruction can be invoked directly (e.g., via a custom client, another program, or a user bypassing the CLI).

### Impact Explanation
If a user (or an integrator building on top of `transfer_to_new_account`) passes `new_authority = Pubkey::default()` — either by mistake, a client-side bug, or malicious construction of the instruction — the resulting new `MarginfiAccount` inherits all the old account's `lending_account` balances (deposits/collateral) while the old account is zeroed out and disabled: [4](#0-3) 

Since no signer can ever match `authority = Pubkey::default()`, none of the standard user instructions (withdraw, repay, close, re-transfer) can be executed against the new account, permanently freezing the transferred funds. This matches the "permanent lock/freeze" impact category.

### Likelihood Explanation
This requires the account's own authority to submit the transfer transaction with an incorrect/zero `new_authority`, so it is not exploitable against another user's funds directly, but it is easily triggered by:
- a buggy or malicious front-end/integrator constructing the instruction directly (bypassing the CLI's explicit `bail!` guard),
- an errant zero/uninitialized value flowing into the instruction from any caller that does not add the CLI's protective check.

Given that the on-chain program is the actual security boundary (the CLI check is not enforced on-chain), likelihood is non-trivial for integrators and third-party callers of this permissionless-style instruction.

### Recommendation
Add an explicit on-chain constraint in `transfer_to_new_account`/`transfer_to_new_account_pda` rejecting `new_authority == Pubkey::default()` (and any other well-known unusable/burn addresses), mirroring the check already present in `p0-cli/src/processor/account.rs`, so the protection cannot be bypassed by direct instruction construction.

### Proof of Concept
1. User calls `transfer_to_new_account` directly (not via `p0-cli`), passing `new_authority = Pubkey::default()`.
2. The instruction succeeds: `initialize_migrated_account` sets `new_account.authority = Pubkey::default()` and copies over `old_account.lending_account` (deposits/collateral).
3. `old_account` is zeroed and flagged `ACCOUNT_DISABLED`.
4. Any subsequent attempt to withdraw/repay/close/transfer from the new account fails `has_one = authority` / `is_signer_authorized` checks, since no valid signer exists for `Pubkey::default()`.
5. All funds held in the new account are permanently locked with no recovery path.

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

**File:** p0-cli/src/processor/account.rs (L1174-1182)
```rust
pub fn marginfi_account_transfer(
    profile: &Profile,
    config: &Config,
    new_authority: Pubkey,
) -> Result<()> {
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
    let authority = config.authority();
```
