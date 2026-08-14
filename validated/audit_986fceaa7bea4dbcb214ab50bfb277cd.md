This confirms the analog. The `new_authority` field in both `TransferToNewAccount` and `TransferToNewAccountPda` account structs is explicitly marked `/// CHECK: WARN: New authority is completely unchecked` [1](#0-0)  and [2](#0-1) , and the handler functions never validate that `new_authority` is non-default before initializing the new account with it and disabling the old one.

### Title
Unchecked zero/default `new_authority` in `transfer_to_new_account`/`transfer_to_new_account_pda` permanently locks the migrated marginfi account - (File: programs/marginfi/src/instructions/marginfi_account/transfer_account.rs)

### Summary
`transfer_to_new_account` and `transfer_to_new_account_pda` let an account's own authority migrate their `MarginfiAccount` to a brand-new account under a caller-supplied `new_authority` pubkey. This parameter is accepted as an `UncheckedAccount` with no on-chain validation that it is non-zero (`Pubkey::default()`), mirroring the reported EthBridge `_to` bug class where an unchecked recipient address led to funds becoming unreachable.

### Finding Description
In `transfer_to_new_account`, `initialize_migrated_account` sets `new_account.authority = new_authority` [3](#0-2)  and then `finalize_migrated_old_account` disables the old account and marks it migrated [4](#0-3) . The handler `transfer_to_new_account` never checks `ctx.accounts.new_authority.key() != Pubkey::default()` before this happens [5](#0-4) . The account struct explicitly documents this account as unchecked: `/// CHECK: WARN: New authority is completely unchecked` [1](#0-0) . The same pattern exists in `transfer_to_new_account_pda` and its accounts struct `TransferToNewAccountPda` [6](#0-5) [2](#0-1) .

Authorization on the resulting account is enforced solely by comparing `marginfi_account.authority == signer` in `is_signer_authorized` [7](#0-6) . Since `Pubkey::default()` (the all-zero pubkey) has no corresponding private key, no one can ever produce a valid `Signer<'info>` matching it — meaning if `new_authority` is set to the zero pubkey, the newly created account (holding the migrated `lending_account` balances, per `initialize_migrated_account`) becomes permanently inaccessible while the old account is simultaneously disabled (`ACCOUNT_DISABLED`) and its `lending_account` zeroed out, so it can no longer be used either.

Notably, the client-side CLI in `p0-cli` *does* perform this check — `if new_authority == Pubkey::default() { bail!("Cannot transfer authority to the zero pubkey"); }` [8](#0-7)  — confirming the protocol team is aware zero is an invalid value, but this guard exists only client-side, not enforced by the on-chain program, exactly matching the analog bug class (client/off-chain guard present, on-chain check missing).

### Impact Explanation
If a user (or a front-end bug, malformed transaction, or malicious relayer front-running with a swapped `new_authority` account) submits `transfer_to_new_account`/`transfer_to_new_account_pda` with `new_authority = Pubkey::default()`, all of the user's collateral and liabilities are moved to a new `MarginfiAccount` PDA/keypair whose `authority` is the zero pubkey. No signer can ever authorize withdrawals, repayments, or closing of that account, and the old account is already disabled and zeroed. This results in permanent loss/freeze of user funds locked in the marginfi position — a permanent lock/freeze of user assets.

### Likelihood Explanation
This is reachable directly by the unprivileged marginfi_account authority themselves (no special privilege needed) since `new_authority` is a fully attacker/user-controlled instruction argument with no additional signer requirement. While a rational user would not intentionally zero their own authority, this can occur due to a client bug, a copy/paste or serialization error building the transaction, or a malicious dApp/relayer substituting the account. Given fee_payer/authority CPI-composability in this protocol, third-party integrators building transactions on behalf of users are also exposed if they fail to replicate the CLI's off-chain zero check.

### Recommendation
Add an explicit on-chain check in both `transfer_to_new_account` and `transfer_to_new_account_pda` requiring `ctx.accounts.new_authority.key() != Pubkey::default()` (mirroring the existing client-side guard in `p0-cli`), returning a dedicated error (e.g. `MarginfiError::InvalidAuthority`) before any account state mutation occurs.

### Proof of Concept
1. Attacker/user calls `transfer_to_new_account` with a legitimate `old_marginfi_account` (owned by them, holding deposits/borrows), a fresh `new_marginfi_account`, and `new_authority = Pubkey::default()`.
2. The instruction passes all existing checks (`ACCOUNT_IN_FLASHLOAN`, `ACCOUNT_IN_RECEIVERSHIP`, `ACCOUNT_IN_ORDER_EXECUTION`, `active_orders == 0`, `migrated_to == default`) since none of them reference `new_authority`.
3. `initialize_migrated_account` sets the new account's `authority` field to `Pubkey::default()` and copies over `lending_account` (all balances/collateral/debt).
4. `finalize_migrated_old_account` disables the old account (`ACCOUNT_DISABLED`) and zeroes its `lending_account`, so it can no longer be used to recover the funds.
5. The new account's balances are now permanently frozen: no keypair exists for `Pubkey::default()`, so `is_signer_authorized` can never return `true` for any withdraw/repay/close instruction on this account.

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

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L51-120)
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

    finalize_migrated_old_account(
        &mut old_account,
        ctx.accounts.new_marginfi_account.key(),
        current_timestamp,
    );

    emit!(MarginfiAccountTransferToNewAccount {
        header: AccountEventHeader {
            signer: Some(ctx.accounts.authority.key()),
            marginfi_account: ctx.accounts.new_marginfi_account.key(),
            marginfi_account_authority: ctx.accounts.new_authority.key(),
            marginfi_group: ctx.accounts.group.key(),
        },
        old_account: ctx.accounts.old_marginfi_account.key(),
        old_account_authority: ctx.accounts.authority.key(),
        new_account_authority: ctx.accounts.new_authority.key(),
    });

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L158-159)
```rust
    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L182-265)
```rust
pub fn transfer_to_new_account_pda(
    ctx: Context<TransferToNewAccountPda>,
    account_index: u16,
    third_party_id: Option<u16>,
) -> MarginfiResult {
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

    // Validate third-party id restriction if provided
    if let Some(id) = third_party_id {
        if !is_allowed_cpi_for_third_party_id(&ctx.accounts.instructions_sysvar, id)? {
            return err!(MarginfiError::Unauthorized);
        }
    }

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

    finalize_migrated_old_account(
        &mut old_account,
        ctx.accounts.new_marginfi_account.key(),
        current_timestamp,
    );

    emit!(MarginfiAccountTransferToNewAccount {
        header: AccountEventHeader {
            signer: Some(ctx.accounts.authority.key()),
            marginfi_account: ctx.accounts.new_marginfi_account.key(),
            marginfi_account_authority: ctx.accounts.new_authority.key(),
            marginfi_group: ctx.accounts.group.key(),
        },
        old_account: ctx.accounts.old_marginfi_account.key(),
        old_account_authority: ctx.accounts.authority.key(),
        new_account_authority: ctx.accounts.new_authority.key(),
    });

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/transfer_account.rs (L312-313)
```rust
    /// CHECK: WARN: New authority is completely unchecked
    pub new_authority: UncheckedAccount<'info>,
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

**File:** p0-cli/src/processor/account.rs (L1179-1181)
```rust
    if new_authority == Pubkey::default() {
        bail!("Cannot transfer authority to the zero pubkey");
    }
```
