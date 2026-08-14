## Analog Vulnerability Confirmed

### Title
Orders can be closed by the account authority while the protocol is paused, bypassing the pause mechanism - (File: `programs/marginfi/src/instructions/marginfi_account/order.rs`)

### Summary
This is a legitimate analog of the reported bug class ("orders can be cancelled while contract is paused"). In marginfi-v2, `place_order`, `start_execute_order`, and `end_execute_order` all gate their `Accounts` structs with `constraint = (!group.load()?.is_protocol_paused()) @ MarginfiError::ProtocolPaused`, but `close_order`'s `CloseOrder` accounts struct has no such check.

### Finding Description
The `CloseOrder` accounts struct only requires `group` for the `has_one` relationship on `marginfi_account`, with no pause-state constraint: [1](#0-0) 

Compare this to `PlaceOrder`, `StartExecuteOrder`, and `EndExecuteOrder`, which explicitly reject the instruction when `group.load()?.is_protocol_paused()` is true: [2](#0-1) [3](#0-2) [4](#0-3) 

The `close_order` handler itself performs no pause validation either — it simply decrements `active_orders` and closes the `Order` account: [5](#0-4) 

This is documented as intentional protocol design elsewhere ("Globally Paused... all financial operations on all Groups and Banks are suspended"), and the project's own guides state that during a pause all user operations should be halted: [6](#0-5) 

`close_order` is a direct analog to the reported `cancelInv()` — the account authority can call it unconditionally, including while `panic_pause` has globally paused the protocol.

### Impact Explanation
Stop-loss/take-profit orders (`Order` accounts) are the mechanism by which a user pre-authorizes a keeper to de-risk their position (close a liability against an asset) if the market moves against them. If the global pause is invoked for an "extreme emergency" (per the README, e.g. an oracle exploit or other incident in progress), a user (or an attacker controlling a compromised/malicious authority) can call `close_order` to cancel their protective stop-loss/take-profit order during the pause window, removing the safety mechanism precisely when the protocol admins intended it to remain in force. This undermines the incident-response guarantee that pausing is supposed to provide, and can leave a position unprotected against adverse price action once the pause is lifted — a state-integrity violation consistent with the original report's classification (bypass of pause protections), even though it does not by itself move funds.

### Likelihood Explanation
High reachability: `close_order` is a permissionless, unprivileged-user instruction requiring only the marginfi account authority's signature; no admin or validator privileges are needed, and the missing constraint is a simple oversight relative to the sibling instructions in the same file that already implement the check. The bug is trivially triggerable any time `panic_pause` is active.

### Recommendation
Add `constraint = (!group.load()?.is_protocol_paused()) @ MarginfiError::ProtocolPaused` to the `group` field in the `CloseOrder` accounts struct, matching the pattern already used in `PlaceOrder`, `StartExecuteOrder`, and `EndExecuteOrder`.

### Proof of Concept
1. Group admin/global fee admin invokes `panic_pause`, setting `MarginfiGroup::is_protocol_paused()` (via `FeeState`/propagated pause flag) to true.
2. A user with an active stop-loss/take-profit `Order` (created via `place_order`) calls `marginfi_account_close_order` (the `CloseOrder` handler) while the pause is active.
3. Because `CloseOrder` has no `is_protocol_paused()` constraint, the transaction succeeds: `active_orders` is decremented and the `Order` account is closed, rent refunded to the caller-supplied `fee_recipient`, even though the protocol is supposed to have all financial/state operations halted.
4. Contrast with attempting `place_order`, `start_execute_order`, or `end_execute_order` during the same pause window, which correctly fail with `MarginfiError::ProtocolPaused`.

### Citations

**File:** programs/marginfi/src/instructions/marginfi_account/order.rs (L139-161)
```rust
pub fn close_order(ctx: Context<CloseOrder>) -> MarginfiResult {
    let CloseOrder {
        marginfi_account: marginfi_account_loader,
        authority,
        order: order_loader,
        ..
    } = &ctx.accounts;

    let mut marginfi_account = marginfi_account_loader.load_mut()?;
    marginfi_account.decrement_active_orders()?;

    emit!(MarginfiAccountCloseOrderEvent {
        header: AccountEventHeader {
            signer: Some(authority.key()),
            marginfi_account: marginfi_account_loader.key(),
            marginfi_account_authority: marginfi_account.authority,
            marginfi_group: marginfi_account.group,
        },
        order: order_loader.key(),
    });

    Ok(())
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/order.rs (L513-517)
```rust
pub struct PlaceOrder<'info> {
    #[account(
        constraint = (!group.load()?.is_protocol_paused()) @ MarginfiError::ProtocolPaused
    )]
    pub group: AccountLoader<'info, MarginfiGroup>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/order.rs (L582-615)
```rust
#[derive(Accounts)]
pub struct CloseOrder<'info> {
    pub group: AccountLoader<'info, MarginfiGroup>,

    #[account(
        mut,
        has_one = group @ MarginfiError::InvalidGroup,
        constraint = {
            let a = marginfi_account.load()?;
            account_not_frozen_for_authority(&a, authority.key())
        } @ MarginfiError::AccountFrozen,
        constraint = {
            let a = marginfi_account.load()?;
            let g = group.load()?;
            is_signer_authorized(&a, g.admin, authority.key(), false, false)
        } @ MarginfiError::Unauthorized
    )]
    pub marginfi_account: AccountLoader<'info, MarginfiAccount>,

    pub authority: Signer<'info>,

    #[account(
        mut,
        has_one = marginfi_account,
        close = fee_recipient
    )]
    pub order: AccountLoader<'info, Order>,

    /// CHECK: no checks whatsoever, marginfi account authority decides this without restriction
    #[account(mut)]
    pub fee_recipient: UncheckedAccount<'info>,

    pub system_program: Program<'info, System>,
}
```

**File:** programs/marginfi/src/instructions/marginfi_account/order.rs (L660-664)
```rust
pub struct StartExecuteOrder<'info> {
    #[account(
        constraint = (!group.load()?.is_protocol_paused()) @ MarginfiError::ProtocolPaused
    )]
    pub group: AccountLoader<'info, MarginfiGroup>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/order.rs (L726-730)
```rust
pub struct EndExecuteOrder<'info> {
    #[account(
        constraint = (!group.load()?.is_protocol_paused()) @ MarginfiError::ProtocolPaused
    )]
    pub group: AccountLoader<'info, MarginfiGroup>,
```

**File:** README.md (L384-389)
```markdown
### Globally Paused

The Global Fee State Admin, which controls program-level fees, can pause the protocol for up to 30
minutes at a time, and up to twice per day. This is used only in extreme emergencies: it has never
been used once as of November 2025. During a global pause, all financial operations on all Groups
and Banks are suspended. 
```
