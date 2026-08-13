## Title
Front-running the admin `MarginfiAccountSetFreeze` (freeze) instruction via `transfer_to_new_account` / `transfer_to_new_account_pda` allows an account authority to evacuate all positions before the freeze lands - (File: `programs/marginfi/src/instructions/marginfi_account/transfer_account.rs`)

### Summary
The group admin can freeze a `MarginfiAccount` via `marginfi_account_set_freeze`, which sets the `ACCOUNT_FROZEN` bit and is meant to lock the account authority out of all further operations on that specific account. [1](#0-0)  Because this freeze targets a specific account (identified by its pubkey) rather than the underlying authority/wallet, a user who is aware the freeze transaction is inbound (e.g. monitoring the public mempool) can front-run it by calling `transfer_to_new_account` or `transfer_to_new_account_pda`, moving every balance to a brand-new account under an authority the user still controls before the freeze transaction confirms. [2](#0-1) 

### Finding Description
`transfer_to_new_account` is a normal, unprivileged, account-authority-callable instruction with no admin gating. [3](#0-2)  Its only pre-conditions are that the account is not mid-flashloan, not in receivership, not mid-order-execution, has no active orders, and has not already been migrated. [4](#0-3)  Critically, it does **not** check `ACCOUNT_FROZEN` before executing — because at the moment the front-running transaction is submitted, the freeze bit has not yet been set on-chain.

The freeze mechanism itself is explicitly account-scoped, not authority-scoped: `Frozen (Bit 6)` blocks "the account's authority" from acting on that particular `MarginfiAccount`, while the group admin retains access to that same account for remediation. [5](#0-4)  If the authority migrates all balances out to a new `MarginfiAccount` (with the same or a different authority they control) before the freeze transaction lands, the old account becomes both frozen and disabled/empty (`ACCOUNT_DISABLED` is set on the old account as part of migration), while the newly created account is fully unaffected by the freeze and holds all of the user's funds. [6](#0-5) 

This is the exact analog of the external report's scenario: an admin decides to freeze/seize control of a suspicious account, and the user races the freeze transaction with a transfer that moves the underlying value to an address outside the admin's reach, rendering the freeze functionally useless for its intended purpose (compliance/investigation/seizure).

### Impact Explanation
The freeze feature is documented as being "used for compliance, investigations, or protecting accounts in unusual situations," and explicitly grants the group admin remediation/seizure access over the frozen account's balances. [7](#0-6)  A successful front-run permanently defeats that remediation/seizure capability: the admin is left with control over an empty, disabled account, while the user's assets are relocated to a new, unfrozen account. This is an unauthorized bypass of an intended access-control/state-restriction mechanism with a permanent effect (the admin cannot re-apply the freeze to recover the funds once they've moved), matching the "unauthorized state change" / bypass-of-a-security-control impact class.

### Likelihood Explanation
Medium — it requires the user to detect or anticipate the freeze transaction (e.g., via mempool monitoring or general expectation of enforcement action) and to hold a valid, unblocked account state (no active orders, not already migrated, not mid-flashloan/receivership) at that moment, all of which are attacker-controlled/unprivileged conditions that are trivial to satisfy in advance.

### Recommendation
Do not rely solely on transaction-ordering to enforce a freeze. Options include: (1) requiring a short admin-set "pending freeze" flag that blocks `transfer_to_new_account`/`transfer_to_new_account_pda` (and other value-moving instructions) as soon as it's set, applied atomically together with the freeze in the same instruction/slot where possible; (2) executing freeze transactions through a private/relayed channel to avoid mempool exposure; or (3) making the freeze effective retroactively/atomically by having the freeze instruction itself also validate that no competing transfer occurred in the same block, though this is generally impractical on Solana. At minimum, document this as a known limitation of the freeze feature so admins understand that a targeted account can preemptively evacuate value if it detects an incoming freeze.

### Proof of Concept
1. Admin observes account `A` (authority `U`) behaving suspiciously and prepares/submits a `marginfi_account_set_freeze(frozen=true)` transaction targeting account `A`. [8](#0-7) 
2. `U`, monitoring the mempool, immediately submits a `transfer_to_new_account` (or `transfer_to_new_account_pda`) instruction on account `A`, specifying a `new_account` with `new_authority` set to a wallet `U` controls. [2](#0-1) 
3. If `U`'s transfer lands first (or in the same slot with higher priority), all of account `A`'s balances move to the new account, and `A` is marked `ACCOUNT_DISABLED`. [6](#0-5) 
4. The admin's freeze transaction still succeeds (since it only checks `group.admin` and does not check whether the account was just emptied), setting `ACCOUNT_FROZEN` on now-empty, already-disabled account `A`. [8](#0-7) 
5. `U` continues operating normally on the new account, which is unaffected by the freeze applied to `A`.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/freeze.rs (L1-19)
```rust
/// Admin-only instruction to toggle `ACCOUNT_FROZEN` on a marginfi account.
///
/// Behavior:
/// - When frozen, the account authority is blocked from major actions (borrow/deposit/withdraw/repay/transfer/etc.) with `AccountFrozen`.
/// - The group admin retains access to operate the account while frozen (for remediation/seizure).
/// - Setting `frozen = false` clears the flag and returns control to the authority under normal auth rules.
pub fn set_account_freeze(ctx: Context<SetAccountFreeze>, frozen: bool) -> MarginfiResult {
    let group = ctx.accounts.group.load()?;
    check_eq!(
        group.admin,
        ctx.accounts.admin.key(),
        MarginfiError::Unauthorized
    );
    let mut marginfi_account = ctx.accounts.marginfi_account.load_mut()?;
    if frozen {
        marginfi_account.set_flag(ACCOUNT_FROZEN, true);
    } else {
        marginfi_account.unset_flag(ACCOUNT_FROZEN, true);
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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L51-99)
```rust
pub fn transfer_to_new_account(ctx: Context<TransferToNewAccount>) -> MarginfiResult {
    // Validate the global fee wallet and claim a nominal fee
    let group = ctx.accounts.group.load()?;
    check_eq!(
        ctx.accounts.global_fee_wallet.key(),
        group.fee_state_cache.global_fee_wallet,
        MarginfiError::InvalidFeeAta
    );
    anchor_lang::system_program::transfer(ctx.accounts.transfer_fee(), ACCOUNT_TRANSFER_FEE)?;

    let mut old_account = ctx.accounts.old_marginfi_account.load_mut()?;

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

**File:** programs/marginfi/src/lib.rs (L496-500)
```rust
    /// (account authority) Transfer all positions to a new account under a new authority. The old
    /// account is disabled. Pays a flat SOL fee to the protocol.
    pub fn transfer_to_new_account(ctx: Context<TransferToNewAccount>) -> MarginfiResult {
        marginfi_account::transfer_to_new_account(ctx)
    }
```

**File:** guides/DEVELOPERS_INTEGRATORS/ACCOUNT_LIFECYCLE.md (L65-74)
```markdown
### Frozen (Bit 6)

- **Flag**: `ACCOUNT_FROZEN` (value 64)
- **Set by**: Group admin via `MarginfiAccountSetFreeze`
- **Cleared by**: Group admin via `MarginfiAccountSetFreeze`
- **Effect**: The account's authority is completely blocked. Only the group admin can perform
  operations on the account. This is used for compliance, investigations, or protecting accounts.

A frozen account's positions continue to accrue interest and can still be liquidated if unhealthy.
The freeze only blocks the authority from interacting.
```
