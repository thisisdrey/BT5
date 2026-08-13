### Title
Freeze bypass via order-execution path: `account_not_frozen_for_authority` only blocks the literal `authority` signer, not delegated execution - (File: `programs/marginfi/src/state/marginfi_account.rs`)

### Summary
`account_not_frozen_for_authority` blocks a frozen account's operations only when the transaction's `authority` signer key literally equals the account's stored `authority` field. `lending_account_withdraw` (and `repay`) pass `allow_order_execution = true` to `is_signer_authorized`, which returns `true` for **any** signer whenever `ACCOUNT_IN_ORDER_EXECUTION` is set, regardless of the freeze flag. Because the two checks are independent constraints combined with `&&`-like short-circuit logic per-check (not a unified authorization state machine), a frozen account whose limit order was placed before the freeze can still have funds withdrawn during order execution by a non-authority signer.

### Finding Description
`account_not_frozen_for_authority` is defined as: [1](#0-0) 
It returns `false` (blocked) **only** when `ACCOUNT_FROZEN` is set **and** `marginfi_account.authority == signer`. If the transaction's `authority` account is any other pubkey, this check passes trivially even though the account is frozen.

`is_signer_authorized` independently allows any signer when the account is in order execution, checked *before* the frozen-flag branch: [2](#0-1) 

`LendingAccountWithdraw` wires both checks together with `allow_receivership = true, allow_order_execution = true`: [3](#0-2) 
The doc comment on the same struct explicitly acknowledges: "during receivership and order execution, there are no signer checks whatsoever: any key can repay as long as the invariants checked at the end of execution are met." [4](#0-3) 

The exploit sequence:
1. Account authority places a limit order while the account is unfrozen (order placement paths such as `CloseOrder`/`PlaceOrder` do enforce `account_not_frozen_for_authority` for the *placing* authority, so this step is legitimate).
2. Group admin later freezes the account via `set_account_freeze`, setting `ACCOUNT_FROZEN`: [5](#0-4) 
3. A keeper/attacker executes the still-pending order, which sets `ACCOUNT_IN_ORDER_EXECUTION` and drives a composed `lending_account_withdraw` call, supplying an `authority` signer that is **not** the account's stored authority (e.g. the keeper's own key or an arbitrary key).
4. `account_not_frozen_for_authority` passes because `signer != marginfi_account.authority`.
5. `is_signer_authorized` passes because `ACCOUNT_IN_ORDER_EXECUTION` short-circuits to `true` for any signer.
6. The withdrawal executes and moves value out of a frozen account — precisely the class of action `ACCOUNT_FROZEN` is documented to block ("the account's authority is completely blocked... only the group admin can perform operations on the account").

This is a genuine "part of the state machine reflects frozen" scenario: the `ACCOUNT_FROZEN` bit is set, but the order-execution sub-state, once entered (from a pre-freeze valid order), is not re-validated against the freeze flag, and the freeze-blocking function keys off signer identity rather than off the frozen flag alone.

### Impact Explanation
This allows unauthorized state mutation (withdrawal of funds) from an account the group admin explicitly froze for compliance/investigation/protection purposes, defeating the stated purpose of the freeze feature ("completely blocked... only the group admin can perform operations"). This matches the "High: unauthorized state mutation or bypass of safety freeze with financial effect" impact category, since it lets already-placed limit orders continue draining/moving funds after a freeze is applied, circumventing the admin's remediation/seizure action.

### Likelihood Explanation
Requires: (1) an order pre-placed before the freeze (feasible for any user account with a limit order feature enabled), (2) the group admin subsequently freezing the account (an external event outside attacker control, but plausible in compliance/incident scenarios — precisely the scenario the freeze feature exists for), and (3) a keeper/attacker permissionlessly triggering order execution afterward, which is by design open to any caller. No admin/governance keys are needed by the attacker; only the freeze event itself is admin-driven, and the attacker only needs to execute a pre-existing, already-authorized order. This is realistically triggerable and repeatable for any account with open orders at freeze time.

### Recommendation
Make `account_not_frozen_for_authority` (or the composed authorization check used by value-moving instructions) check the `ACCOUNT_FROZEN` flag independently of signer identity when the operation is reached via the order-execution or receivership bypass paths, i.e., block *all* callers (not just the literal authority) once `ACCOUNT_FROZEN` is set, unless the caller is the group admin. Concretely, `is_signer_authorized` should check `ACCOUNT_FROZEN` before the `allow_order_execution`/`allow_receivership` short-circuits (or `execute_order` should re-validate that the target account is not frozen before entering order-execution mode), and `account_not_frozen_for_authority` should not be satisfied merely because the signer differs from the stored authority.

### Proof of Concept
Rust integration test plan (extending `programs/marginfi/tests/admin_actions/freeze.rs` patterns):
1. Create a marginfi account, deposit collateral, and place a limit/take-profit order via `PlaceOrder` while unfrozen — order execution becomes eligible.
2. Admin calls `set_account_freeze(frozen = true)` on the account.
3. As an unrelated keypair (not the account authority), submit the order-execution instruction that internally invokes `lending_account_withdraw`/`repay`/`deposit` with `ACCOUNT_IN_ORDER_EXECUTION` set and `authority` = the keeper's own key.
4. Assert the transaction **succeeds** (expected: it should fail with `AccountFrozen`, but current code allows it), and assert token balances moved out of the frozen account's vault.
5. Compare against `frozen_account_blocks_withdraw_allows_admin` test which shows the direct-authority withdraw path correctly returns `MarginfiError::AccountFrozen`: [6](#0-5)  — the PoC should show the order-execution path does not receive the same rejection.

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

**File:** programs/marginfi/src/instructions/marginfi_account/withdraw.rs (L278-282)
```rust
    /// Must be marginfi_account's authority, unless in liquidation/deleverage receivership or order execution
    ///
    /// Note: during receivership and order execution, there are no signer checks whatsoever: any key can repay as
    /// long as the invariants checked at the end of execution are met.
    pub authority: Signer<'info>,
```

**File:** programs/marginfi/src/instructions/marginfi_account/freeze.rs (L7-20)
```rust
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
    marginfi_account.last_update = Clock::get()?.unix_timestamp as u64;
```

**File:** programs/marginfi/tests/admin_actions/freeze.rs (L184-189)
```rust
    marginfi_account.try_set_freeze(true).await?;

    let res = marginfi_account
        .try_bank_withdraw_with_authority(user_token_account.key, &usdc_bank, 1.0, None, &authority)
        .await;
    assert_custom_error!(res.unwrap_err(), MarginfiError::AccountFrozen);
```
