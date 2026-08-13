No vulnerability found for this question.

**Analysis summary:**

The `lending_account_withdraw` accounts struct enforces both `account_not_frozen_for_authority` and `is_signer_authorized` as ANDed constraints [1](#0-0) . `account_not_frozen_for_authority` only blocks the case where the frozen account's own `authority` field matches the signer [2](#0-1) , but `is_signer_authorized` independently re-checks the frozen flag and requires `group_admin == signer` whenever the account is frozen and not under an explicitly allowed receivership/order-execution path [3](#0-2) . Since both constraints must pass, a non-admin/non-authority signer cannot withdraw from a frozen account through the normal path.

The one case where a non-authority, non-admin signer can bypass the frozen-admin-only rule is when the account is simultaneously in receivership (`ACCOUNT_IN_RECEIVERSHIP`), because `is_signer_authorized` checks receivership before the frozen flag [4](#0-3) . This is explicitly documented as intended behavior: frozen accounts remain liquidatable, and freezing only blocks the account's own authority, not the liquidation/receivership mechanism . This interaction is a designed feature, not an unintended bypass — receivership entry itself requires the account to be unhealthy (a separate gate enforced by `StartLiquidation`), so it does not constitute an unprivileged attacker gaining unauthorized control of a healthy frozen account.

For the migration/transfer path (`transfer_account.rs`), the frozen flag (`account_flags`) is copied verbatim from the old account into the new migrated account, so freeze status is preserved across migration, and the transfer instruction itself is gated by the same `account_not_frozen_for_authority` + `is_signer_authorized` pair, blocking a frozen account's own authority from migrating out [5](#0-4) [6](#0-5) . No stale-authority or partially-completed migration state was found that would let an attacker finish a transition with the wrong signer.

No code path was found where `lending_account_withdraw` accepts a wrong signer on a frozen account outside of the documented liquidation/receivership exception, and that exception does not meet the bounty's High-impact bar since it requires the account to already be unhealthy and liquidatable — a pre-existing, independently gated condition, not a freeze-bypass vulnerability.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L259-276)
```rust
    #[account(
        mut,
        has_one = group @ MarginfiError::InvalidGroup,
        constraint = {
            let acc = marginfi_account.load()?;
            !acc.get_flag(ACCOUNT_DISABLED)
        } @MarginfiError::AccountDisabled,
        constraint = {
            let a = marginfi_account.load()?;
            account_not_frozen_for_authority(&a, authority.key())
        } @ MarginfiError::AccountFrozen,
        constraint = {
            let a = marginfi_account.load()?;
            let g = group.load()?;
            is_signer_authorized(&a, g.admin, authority.key(), true, true)
        } @ MarginfiError::Unauthorized
    )]
    pub marginfi_account: AccountLoader<'info, MarginfiAccount>,
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L84-104)
```rust
pub fn is_signer_authorized(
    marginfi_account: &MarginfiAccount,
    group_admin: Pubkey,
    signer: Pubkey,
    allow_receivership: bool,
    allow_order_execution: bool,
) -> bool {
    if allow_receivership && marginfi_account.get_flag(ACCOUNT_IN_RECEIVERSHIP) {
        return marginfi_account.authority != signer; // forbidden to take receivership of your own account
    }

    if allow_order_execution && marginfi_account.get_flag(ACCOUNT_IN_ORDER_EXECUTION) {
        return true;
    }

    if marginfi_account.get_flag(ACCOUNT_FROZEN) {
        return group_admin == signer;
    }

    marginfi_account.authority == signer
}
```

**File:** programs/marginfi/src/state/marginfi_account.rs (L116-121)
```rust
pub fn account_not_frozen_for_authority(
    marginfi_account: &MarginfiAccount,
    signer: Pubkey,
) -> bool {
    !(marginfi_account.get_flag(ACCOUNT_FROZEN) && marginfi_account.authority == signer)
}
```

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L131-144)
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
