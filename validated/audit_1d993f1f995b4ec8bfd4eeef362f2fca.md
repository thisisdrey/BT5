### Title
`admin_close_account` closes accounts without checking `ACCOUNT_FROZEN`, bypassing freeze semantics enforced only in `close_account` - ([File: programs/marginfi/src/instructions/marginfi_account/admin_close.rs])

### Summary
`close_account` explicitly rejects any account with `ACCOUNT_FROZEN` set before even reaching `can_be_closed()` [1](#0-0) , but the alternate, fully permissionless `admin_close_account` entrypoint never re-checks that flag, only re-validating `ACCOUNT_IN_DELEVERAGE`, `ACCOUNT_IN_ORDER_EXECUTION`, `active_orders`, and `liquidation_record` in addition to `can_be_closed()` [2](#0-1) . This asymmetry strongly suggests `can_be_closed()` itself does not encode the frozen-flag guard (otherwise the explicit check in `close_account` would be redundant), meaning a dust-balance, inactive, frozen account can be closed through the "back door" `admin_close_account` path by any unprivileged caller.

### Finding Description
`close_account`'s guard order is: (1) explicit `ACCOUNT_FROZEN` check, (2) `liquidation_record` check, (3) `active_orders` check, (4) `can_be_closed()` [3](#0-2) . The fact that the frozen check is hoisted out and performed separately from `can_be_closed()` implies `can_be_closed()` does not itself enforce the freeze invariant.

`admin_close_account` is documented as a "Permissionless instruction to close legacy or new accounts that are empty and inactive for >60 days," computed "from direct account invariants (balances/flags/timestamps)" [4](#0-3) . Its `Accounts` struct requires no `Signer` at all — only `group`, `marginfi_account` (closed to `global_fee_wallet`), and an unchecked `global_fee_wallet` validated against the group's fee cache [5](#0-4) , making it callable by any unprivileged party (bot/keeper/public caller). Its checks are: `can_be_closed() && is_inactive` and then `!ACCOUNT_IN_DELEVERAGE && !ACCOUNT_IN_ORDER_EXECUTION && active_orders == 0 && liquidation_record == default` [6](#0-5) . Notably absent from this explicit re-check list is `ACCOUNT_FROZEN`, which `close_account` treats as a first-class, independent gate.

If a group admin (or receivership/dispute mechanism) freezes an account to lock its state for investigation while it happens to hold only dust balances and has been inactive for >60 days, any unprivileged caller can invoke `admin_close_account` to close it — bypassing the freeze guard that the "normal" `close_account` flow enforces. This is exactly the alternate-flow / stale-authority reuse pattern the question describes: one code path (`close_account`) enforces the frozen invariant, a second reachable path (`admin_close_account`) that reaches the same terminal state mutation (account closure) does not re-derive or reuse that guard.

### Impact Explanation
Closing an account destroys its on-chain state permanently. If freeze is used as a protective mechanism (e.g., pending liquidation/receivership resolution, dispute, or migrated/stale-authority containment), a permissionless bypass of that freeze via `admin_close_account` results in unauthorized destruction of account state that the protocol intended to keep locked, i.e., "unauthorized state change" and potential stranding of any side-state or invariants tied to that account's continued existence. Because eligibility still requires `can_be_closed()` and 60 days of inactivity, direct value theft is unlikely, but the freeze invariant itself — which the audit question specifically targets — is violated by a reachable, unprivileged, non-admin instruction.

### Likelihood Explanation
Preconditions: the account must be frozen, dust/empty per `can_be_closed()`, and inactive for >60 days. These are plausible for accounts frozen during long-running investigations or after migration where the underlying balances have been drained to near zero. `admin_close_account` requires no signer or special role, making it trivially and repeatably callable by any unprivileged actor once the timing condition is met.

### Recommendation
Add an explicit `!marginfi_account.get_flag(ACCOUNT_FROZEN)` check to `admin_close_account`, mirroring `close_account`, or move the frozen-flag check into `can_be_closed()` itself so every closure path (user-initiated and permissionless admin-close) shares one authoritative closeability guard.

### Proof of Concept
1. Unit/integration test in `programs/marginfi/tests/admin_actions/freeze.rs` style:
   - Create a `MarginfiAccount` with dust/zero balances, set `last_update` to >60 days in the past.
   - Freeze the account via the admin freeze instruction (`programs/marginfi/src/instructions/marginfi_account/freeze.rs`), setting `ACCOUNT_FROZEN`.
   - Call `close_account` as the account authority — assert it fails with `MarginfiError::AccountFrozen`.
   - Call `admin_close_account` (no signer needed) — assert current behavior succeeds (bug), then after fix, assert it fails with `MarginfiError::IllegalAction`/`AccountFrozen`.
2. Confirm via `can_be_closed()`'s actual body (not retrievable in this session due to index truncation — recommend inspecting `programs/marginfi/src/state/marginfi_account.rs` directly in a full checkout) whether `ACCOUNT_FROZEN` is included; if it is already included, this specific finding is moot and the redundant check in `close_account` is defense-in-depth only. This should be verified in a Devin session with full file access before applying the fix.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/close.rs (L6-29)
```rust
pub fn close_account(ctx: Context<MarginfiAccountClose>) -> MarginfiResult {
    let marginfi_account = &ctx.accounts.marginfi_account.load()?;

    if marginfi_account.get_flag(ACCOUNT_FROZEN) {
        return err!(MarginfiError::AccountFrozen);
    }

    check!(
        marginfi_account.liquidation_record == Pubkey::default(),
        MarginfiError::IllegalAction,
        "Close liquidation record before closing account"
    );

    check!(
        marginfi_account.active_orders == 0,
        MarginfiError::IllegalAction,
        "Close all active orders before closing account"
    );

    check!(
        marginfi_account.can_be_closed(),
        MarginfiError::IllegalAction,
        "Account cannot be closed"
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/admin_close.rs (L15-17)
```rust
/// Permissionless instruction to close legacy or new accounts that are empty and inactive for >60
/// days. Eligibility is computed from direct account invariants (balances/flags/timestamps), not
/// indexer flags, so pre-flag accounts remain safely closeable.
```

**File:** programs/marginfi/src/instructions/marginfi_account/admin_close.rs (L18-39)
```rust
pub fn admin_close_account(ctx: Context<AdminCloseAccount>) -> MarginfiResult {
    let marginfi_account = ctx.accounts.marginfi_account.load()?;
    let clock = Clock::get()?;
    let elapsed = clock
        .unix_timestamp
        .saturating_sub(marginfi_account.last_update as i64);
    let is_inactive = elapsed > 60 * SECONDS_PER_DAY;

    check!(
        marginfi_account.can_be_closed() && is_inactive,
        MarginfiError::IllegalAction,
        "Account is not eligible for close (not empty or active within 60d)"
    );

    check!(
        !marginfi_account.get_flag(ACCOUNT_IN_DELEVERAGE)
            && !marginfi_account.get_flag(ACCOUNT_IN_ORDER_EXECUTION)
            && marginfi_account.active_orders == 0
            && marginfi_account.liquidation_record == Pubkey::default(),
        MarginfiError::IllegalAction,
        "Account cannot be closed"
    );
```

**File:** programs/marginfi/src/instructions/marginfi_account/admin_close.rs (L54-72)
```rust
#[derive(Accounts)]
pub struct AdminCloseAccount<'info> {
    pub group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        mut,
        has_one = group @ MarginfiError::InvalidGroup,
        close = global_fee_wallet
    )]
    pub marginfi_account: AccountLoader<'info, MarginfiAccount>,

    /// CHECK: Validated against group fee state cache
    #[account(
        mut,
        constraint = global_fee_wallet.key() == group.load()?.fee_state_cache.global_fee_wallet
            @ MarginfiError::InvalidGlobalFeeWallet
    )]
    pub global_fee_wallet: UncheckedAccount<'info>,
}
```
