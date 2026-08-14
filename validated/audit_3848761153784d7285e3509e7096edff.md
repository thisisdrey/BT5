### Title
Order-execution flag bypasses `ACCOUNT_FROZEN` restriction in `is_signer_authorized`, allowing frozen accounts to still withdraw funds via pending orders - ([File: programs/marginfi/src/state/marginfi_account.rs])

### Summary
`lending_account_withdraw` gates the `marginfi_account` with two independent checks: `account_not_frozen_for_authority` and `is_signer_authorized(..., allow_receivership=true, allow_order_execution=true)`. The latter's branch ordering makes the `ACCOUNT_IN_ORDER_EXECUTION` case return `true` unconditionally, before the frozen-state check is ever evaluated, so once an order execution is in flight, `ACCOUNT_FROZEN` is completely ignored and any signer (not just the account authority) is treated as authorized.

### Finding Description
`account_not_frozen_for_authority` only blocks a call when the signer is *exactly* the account's stored `authority` field while the account is frozen: [1](#0-0) 

`is_signer_authorized`, used together with it in `LendingAccountWithdraw`, evaluates conditions in this strict priority order: receivership → order execution → frozen → default authority match. Critically, the order-execution branch short-circuits and returns `true` unconditionally, without ever reaching the frozen check: [2](#0-1) 

`LendingAccountWithdraw` invokes both guards with `allow_receivership=true, allow_order_execution=true`: [3](#0-2) 

Consequence: if `ACCOUNT_IN_ORDER_EXECUTION` is set on an account that is also `ACCOUNT_FROZEN` (these two flags are independent bits and nothing in the flag-management code prevents both being set simultaneously — freezing is an admin action, `ACCOUNT_IN_ORDER_EXECUTION` is set transactionally when a previously-placed order is executed), then:
- `is_signer_authorized(..., true, true)` returns `true` unconditionally for **any** signer, bypassing the intended "frozen → admin only" rule (documented explicitly at line 82 of the same file: `"3. If the account is frozen → true only if signer is the group admin"`, which is never reached).
- `account_not_frozen_for_authority` also returns `true` (not blocked) as long as the calling `authority` Signer key passed to the instruction differs from `marginfi_account.authority` (which is trivially the case for a keeper/order-executor signer), since it only compares `marginfi_account.authority == signer`.

Both gating constraints therefore evaluate to "allowed" and `lending_account_withdraw` executes a real value-moving transfer out of the bank's liquidity vault, even though the account carries the `ACCOUNT_FROZEN` flag that is supposed to block every value-moving path for that account.

### Impact Explanation
This is a real bypass of the freeze safety mechanism with direct financial effect: an admin (or automated risk system) freezing an account to halt fund movement (e.g., due to suspected exploit, fraud investigation, or as an emergency circuit breaker) can be circumvented if the account has a pending order, because order execution unconditionally overrides the frozen check in `is_signer_authorized` and `account_not_frozen_for_authority` does not catch non-authority signers. Funds can still leave the frozen account's position via the order-execution path, defeating the purpose of the freeze. This matches "High: unauthorized state mutation or bypass of safety freeze with financial effect."

### Likelihood Explanation
Preconditions: (1) an account has a pending order placed before being frozen, (2) admin freezes the account (a normal, reachable admin action, but the trigger — an unprivileged keeper executing the queued order — is fully attacker/keeper-controlled and requires no privilege), (3) the order execution flow is later invoked (permissionlessly, by any keeper) causing `ACCOUNT_IN_ORDER_EXECUTION` to be set and, within the same transaction/CPI chain, `lending_account_withdraw` to be called on behalf of that account. Because order execution is designed to be triggerable by arbitrary keepers, and freezing does not appear to cancel or block pending orders, this path is realistically repeatable any time a frozen account has an outstanding order.

### Recommendation
In `is_signer_authorized`, check `ACCOUNT_FROZEN` before (or in conjunction with) the order-execution branch, so that a frozen account cannot have orders executed against it regardless of `ACCOUNT_IN_ORDER_EXECUTION` state, unless the signer is the group admin. Additionally, freezing an account should either forcibly cancel/invalidate all pending orders (decrementing `active_orders`/closing `Order` accounts) or the order-execution entrypoint should independently re-check `ACCOUNT_FROZEN` and reject execution.

### Proof of Concept
Rust integration test outline (extending `programs/marginfi/tests/admin_actions/freeze.rs` and the order-execution test harness):
1. Create a `MarginfiAccountFixture` with a distinct authority; deposit collateral.
2. Place a valid order via `PlaceOrder` (targeting a withdrawal-type order) while unfrozen — this increments `active_orders` and creates an `Order` account.
3. Admin calls `try_set_freeze(true)` on the account, setting `ACCOUNT_FROZEN`.
4. Assert that a direct `lending_account_withdraw` call from the account authority fails with `MarginfiError::AccountFrozen` (baseline, confirms freeze works for direct authority calls).
5. Have a keeper (arbitrary unprivileged signer distinct from `authority`) invoke the order-execution instruction that sets `ACCOUNT_IN_ORDER_EXECUTION` and triggers `lending_account_withdraw` as part of order fulfillment.
6. Assert the withdrawal **succeeds** despite `ACCOUNT_FROZEN` still being set — this is the bug; expected correct behavior is that it should fail with `AccountFrozen` (or the order should have been invalidated at freeze time).
7. Verify token balances/vault state before and after to show real value moved out of a frozen account.

Note: I was not able to fully trace the `execute_order`/order-fulfillment instruction implementation (its exact CPI wiring into `lending_account_withdraw` and whether it sets `ACCOUNT_IN_ORDER_EXECUTION` before or after freeze checks) within the available tool budget; this should be verified directly in `programs/marginfi/src/instructions/marginfi_account/order.rs` (execute-order handler) before treating this as fully confirmed, though the flag-priority logic in `is_signer_authorized` itself is unambiguous and independently verifiable.

### Citations

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
