### Title
DeleteAccount action burns beneficiary refund when the beneficiary account does not exist, permanently destroying user funds - (File: runtime/runtime/src/actions.rs)

### Summary
`validate_delete_action` only checks that `beneficiary_id` is a syntactically valid account id via `validate_action_account_id` [1](#0-0) ; it never checks that the beneficiary account actually exists. `action_delete_account` then unconditionally queues a `Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance)` for the full account balance and irreversibly deletes the account (`*account = None`) in the same step [2](#0-1) . This mirrors the Vault.sol `blacklistProtocol()` pattern: an action that finalizes an irreversible state change (deleting the account / blacklisting the protocol) without first confirming that the associated fund-transfer will actually succeed.

### Finding Description
When the delete-account refund receipt is later processed, it is a system-predecessor `Transfer` action to `beneficiary_id`. If `beneficiary_id` does not exist and is not implicit-account-creation-eligible, `check_account_existence` routes into `check_transfer_to_nonexisting_account`, which explicitly documents: "Refunds don't automatically create accounts, because refunds are free... Account deletion with beneficiary creates a refund, so it'll not create a new account" [3](#0-2) . Consequently the refund action fails with `AccountDoesNotExist`.

Per the documented invariant, "**Refund receipts are free**: system-predecessor receipts burn zero gas; a failed refund burns its deposit into `other_burnt_amount` rather than refunding" [4](#0-3) . So the entire deleted account's balance is silently and permanently burnt instead of being returned to the deleting user or the (non-existent) beneficiary — with no error surfaced back to the original `DeleteAccount` transaction, since that transaction already succeeded and the account was already removed by the time the refund receipt executes.

This is exactly analogous to the Vault.sol bug: `blacklistProtocol()` irreversibly marks state (blacklisted) based on an unchecked assumption that the withdrawal fully succeeded, leaving funds unrecoverable. Here, `action_delete_account` irreversibly destroys the account state based on the unchecked assumption that the beneficiary transfer will succeed, leaving the account's balance permanently burnt when it doesn't.

### Impact Explanation
Any ordinary user can trigger permanent loss of their own account balance by submitting a `DeleteAccountAction` with a `beneficiary_id` that is syntactically valid but does not exist (e.g., a mistyped account, an account that was itself deleted between construction and execution of the transaction, or a deliberately unregistered top-level name). Because the beneficiary refund is a receipt executed asynchronously after the account is already gone, there is no way to detect or roll back the failure at the point of signing — the funds are burnt rather than refunded, satisfying "concrete... permanent freezing of funds" (in this case funds are destroyed/burnt rather than merely stuck, which is a strictly worse outcome for the user).

### Likelihood Explanation
Reachable directly and deterministically by any account owner via a standard `DeleteAccount` action — no special privileges, validator, or node compromise required. The only requirement is a `beneficiary_id` that passes `AccountId` syntax validation but does not resolve to an existing account at the time the refund receipt executes (race condition or simple user error make this trivially achievable, and it can also be triggered maliciously against oneself or via tooling bugs that pick a bad beneficiary).

### Recommendation
Before allowing `action_delete_account` to proceed, verify that `beneficiary_id` refers to an existing account (or is implicit-account-creation eligible) — analogous to the fix pattern recommended for the Vault.sol bug (verify the "withdrawal" target/path will succeed before finalizing the irreversible action). Alternatively, change the failed-refund path so that a failed beneficiary transfer from an account-deletion refund does not silently burn funds (e.g., escalate to a fatal error, or fall back to returning funds to a recoverable account) rather than routing them into `other_burnt_amount`.

### Proof of Concept
1. Create account `alice.near` with balance `B`.
2. From `alice.near`, submit a transaction with a single `DeleteAccount { beneficiary_id: "nonexistent999.near" }` action, where `nonexistent999.near` is a syntactically valid, never-created account id.
3. `validate_delete_action` passes (only format is checked) [1](#0-0) .
4. `action_delete_account` executes: it pushes `Receipt::new_balance_refund(beneficiary_id, B)` and sets `*account = None`, deleting `alice.near` [2](#0-1) .
5. On the next receipt-processing step, the refund receipt targets `nonexistent999.near`; `check_account_existence`/`check_transfer_to_nonexisting_account` rejects it (refunds cannot create accounts) [3](#0-2) .
6. The failed refund's deposit `B` is burnt into `other_burnt_amount` instead of being credited anywhere, per the documented refund-receipt failure behavior [4](#0-3) .
7. Result: `alice.near`'s entire balance `B` is permanently destroyed with no recovery path, and no error is visible on the original signed transaction (which already succeeded).

### Citations

**File:** runtime/runtime/src/action_validation.rs (L399-403)
```rust
fn validate_delete_action(action: &DeleteAccountAction) -> Result<(), ActionsValidationError> {
    validate_action_account_id(&action.beneficiary_id)?;

    Ok(())
}
```

**File:** runtime/runtime/src/actions.rs (L364-371)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
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

**File:** protocol-model/spec/runtime-execution.md (L152-152)
```markdown
- **Refund receipts are free**: system-predecessor receipts burn zero gas; a failed refund burns its deposit into `other_burnt_amount` rather than refunding (`runtime/runtime/src/lib.rs:929`, `:972`).
```
