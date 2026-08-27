All the mechanics described in the audit are confirmed by the code. This is a real, well-documented behavior (explicitly commented in the source as intentional), not a hidden bug — but it does constitute a genuine fund-loss primitive reachable by an unprivileged attacker against their own account.

### Title
Unregistered `beneficiary_id` in `DeleteAccountAction` causes irrecoverable burn of the deleted account's entire balance - (File: `runtime/runtime/src/action_validation.rs`, `runtime/runtime/src/actions.rs`, `runtime/runtime/src/lib.rs`)

### Summary
`validate_delete_action` only checks that `beneficiary_id` is a syntactically valid `AccountId` via `validate_action_account_id`, never whether the account exists or is reachable. `action_delete_account` unconditionally creates a `Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance)` with `predecessor_id = "system"`. If `beneficiary_id` is a syntactically valid but non-existent, non-implicit-format account, the resulting Transfer receipt fails at `check_account_existence`/`check_transfer_to_nonexisting_account`, and because refund receipts are exempted from generating further refunds on failure, the deposit is burned into `other_burnt_amount` instead of being returned anywhere.

### Finding Description
`validate_delete_action` in `runtime/runtime/src/action_validation.rs`:399-403 does only:
```rust
fn validate_delete_action(action: &DeleteAccountAction) -> Result<(), ActionsValidationError> {
    validate_action_account_id(&action.beneficiary_id)?;
    Ok(())
}
``` [1](#0-0) 

This is purely a syntax check (format/length rules for `AccountId`), with no existence or reachability check. When the `DeleteAccount` action executes, `action_delete_account` (`runtime/runtime/src/actions.rs`) does:
```rust
let account_balance = account_ref.amount();
if account_balance > Balance::ZERO {
    result.new_receipts.push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
}
``` [2](#0-1) 

`Receipt::new_balance_refund` builds a receipt with `predecessor_id: "system"` and a single `Transfer` action for the full balance: [3](#0-2) 

When this refund receipt is later applied, `apply_action` computes `is_refund = receipt.predecessor_id().is_system()` and sets `implicit_account_creation_eligible = is_the_only_action && !is_refund` — i.e., implicit account auto-creation is explicitly disabled for refund receipts: [4](#0-3) 

If the beneficiary account does not exist, `check_account_existence` routes to `check_transfer_to_nonexisting_account`, which explicitly documents that refunds never auto-create accounts and returns `AccountDoesNotExist` for any non-implicit-eligible transfer to a nonexistent account: [5](#0-4) 

Because the receipt fails and `predecessor_id().is_system()` is true, `apply_action_receipt` takes the "refund receipts are free" branch: no new refund receipt is generated, and the entire deposit is instead added to `other_burnt_amount`:
```rust
let gas_refund_result = if receipt.predecessor_id().is_system() {
    if result.result.is_err() {
        stats.balance.other_burnt_amount = safe_add_balance(
            stats.balance.other_burnt_amount,
            total_deposit(&action_receipt.actions())?,
        )?
    }
    GasRefundResult::default()
} else { ... }
``` [6](#0-5) 

The failed receipt then rolls back all state changes except the already-deducted burnt amount accounting (`state_update.rollback()`), but the original account was already removed by `remove_account` inside the (separately committed) `DeleteAccount` receipt, so the funds are gone with no path back to the original owner.

### Impact Explanation
An attacker's own funds are permanently destroyed via a single self-inflicted transaction: send `DeleteAccountAction { beneficiary_id: <syntactically valid, never-registered, non-implicit account name> }` on their own account. This matches the "permanent freezing/loss of user funds" bounty category, though it is self-targeted (loss of the attacker's own balance, not third-party theft) — impact is limited to funds the caller voluntarily routes to a bogus beneficiary. There is no cross-account theft primitive here since the beneficiary must be named directly and truthfully by the deleting account holder.

### Likelihood Explanation
Trivial to trigger and fully repeatable: a normal signed transaction with a `DeleteAccount` action whose `beneficiary_id` is any syntactically valid, unregistered account name (e.g., `"nonexistent12345.near"`). No special privileges needed beyond owning the account being deleted. However, this is exclusively self-damaging — a rational attacker has no incentive to burn their own balance, and it cannot be used to steal funds from any other account since `beneficiary_id` doesn't affect the actor's own state destructively for anyone but the caller. The primary residual risk is user error (fat-fingering a beneficiary account) rather than an "attack," which is also explicitly documented/acknowledged behavior in the code comment: "Refunds don't automatically create accounts, because refunds are free and we don't want some type of abuse... Account deletion with beneficiary creates a refund, so it'll not create a new account" (`runtime/runtime/src/actions.rs:869-872`).

### Recommendation
This is treated as intended/documented protocol behavior (see the comment in `check_transfer_to_nonexisting_account`), not a bug to silently patch. If loss-of-user-funds risk is a concern, consider either (a) validating at submission time (in `validate_delete_action` or client tooling) that `beneficiary_id` refers to an existing or implicit-format account before accepting the transaction, or (b) documenting this hazard prominently in wallet/CLI UX so users don't accidentally lose funds. A protocol-level fix (e.g., allowing implicit-account auto-creation specifically for `DeleteAccount` balance refunds) would be a consensus-affecting change requiring a protocol feature gate and careful review of the "refunds are free" invariant it currently relies on.

### Proof of Concept
Integration test in `test-loop-tests` (mirroring `test_instant_delete_account`):
1. Fund `account0` with balance `B`.
2. Submit `DeleteAccount { beneficiary_id: "nonexistent999.near" }` (syntactically valid, never registered, not implicit format) as the sole/final action from `account0`.
3. Assert the parent `DeleteAccount` receipt outcome is `SuccessReceiptId` (deletion itself succeeds) and `account0` no longer exists.
4. Assert the child balance-refund receipt (predecessor `"system"`, receiver `nonexistent999.near`) fails with `ActionErrorKind::AccountDoesNotExist`.
5. Assert `nonexistent999.near` was never created (`view_account` fails).
6. Assert via `ChunkApplyStatsV1`/`other_burnt_amount` (or by summing total supply before/after) that `B` yoctoNEAR was burned rather than refunded anywhere — confirming permanent, unrecoverable loss.

### Citations

**File:** runtime/runtime/src/action_validation.rs (L399-403)
```rust
fn validate_delete_action(action: &DeleteAccountAction) -> Result<(), ActionsValidationError> {
    validate_action_account_id(&action.beneficiary_id)?;

    Ok(())
}
```

**File:** runtime/runtime/src/actions.rs (L364-370)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
```

**File:** runtime/runtime/src/actions.rs (L857-877)
```rust
fn check_transfer_to_nonexisting_account(
    config: &RuntimeConfig,
    account_id: &AccountId,
    implicit_account_creation_eligible: bool,
) -> Result<(), ActionError> {
    if implicit_account_creation_eligible
        && account_is_implicit(account_id, config.wasm_config.eth_implicit_accounts)
    {
        // OK. It's implicit account creation.
        // Notes:
        // - Transfer action has to be the only action in the transaction to avoid
        // abuse by hijacking this account with other public keys or contracts.
        // - Refunds don't automatically create accounts, because refunds are free and
        // we don't want some type of abuse.
        // - Account deletion with beneficiary creates a refund, so it'll not create a
        // new account.
        Ok(())
    } else {
        Err(ActionErrorKind::AccountDoesNotExist { account_id: account_id.clone() }.into())
    }
}
```

**File:** core/primitives/src/receipt.rs (L496-510)
```rust
    pub fn new_balance_refund(receiver_id: &AccountId, refund: Balance) -> Self {
        Receipt::V0(ReceiptV0 {
            predecessor_id: "system".parse().unwrap(),
            receiver_id: receiver_id.clone(),
            receipt_id: CryptoHash::default(),
            receipt: ReceiptEnum::Action(ActionReceipt {
                signer_id: "system".parse().unwrap(),
                signer_public_key: PublicKey::empty(KeyType::ED25519),
                gas_price: Balance::ZERO,
                output_data_receivers: vec![],
                input_data_ids: vec![],
                actions: vec![Action::Transfer(TransferAction { deposit: refund })],
            }),
        })
    }
```

**File:** runtime/runtime/src/lib.rs (L547-562)
```rust
        let account_id = receipt.receiver_id();
        let is_refund = receipt.predecessor_id().is_system();
        let is_the_only_action = actions.len() == 1;
        let implicit_account_creation_eligible = is_the_only_action && !is_refund;

        // Account validation
        if let Err(e) = check_account_existence(
            action,
            account,
            account_id,
            &apply_state.config,
            implicit_account_creation_eligible,
        ) {
            result.result = Err(e);
            return Ok(result);
        }
```

**File:** runtime/runtime/src/lib.rs (L993-1001)
```rust
        let gas_refund_result = if receipt.predecessor_id().is_system() {
            // If the refund fails tokens are burned.
            if result.result.is_err() {
                stats.balance.other_burnt_amount = safe_add_balance(
                    stats.balance.other_burnt_amount,
                    total_deposit(&action_receipt.actions())?,
                )?
            }
            GasRefundResult::default()
```
