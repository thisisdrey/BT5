### Title
`action_delete_account` permits `beneficiary_id == account_id`, permanently burning the deleted account's balance instead of transferring it - (File: runtime/runtime/src/actions.rs)

### Summary
`DeleteAccountAction { beneficiary_id }` is never checked to ensure the `beneficiary_id` differs from the account being deleted. `action_validation.rs` only enforces that `DeleteAccount` is the last action in a receipt and validates `beneficiary_id` as a syntactically valid `AccountId` — it never checks it isn't the very account about to be removed. Because the balance-refund receipt is dispatched *after* the account is deleted, choosing the deleted account itself as beneficiary sends the funds to a target that no longer exists at execution time, and per the refund model, a failed refund receipt is burned rather than re-refunded. This is the same bug class as the external report: a critical, user-settable destination parameter is missing a "not this address/entity" sanity check, letting an ordinary, easily-triggered mistake permanently destroy funds.

### Finding Description
`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) does the following, in order:
1. Computes `account_balance = account_ref.amount()`.
2. If positive, pushes `Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance)` — a *new*, separately-processed receipt targeting `beneficiary_id`. [1](#0-0) 
3. Only then does it call `remove_account(state_update, account_id)?` and set `*account = None`, actually deleting the account from state. [2](#0-1) 

There is no check anywhere that `delete_account.beneficiary_id != account_id`. The validation path in `action_validation.rs` only checks `DeleteAccount` is the final action in a receipt (`DeleteActionMustBeFinal`) and, per docs, that `beneficiary_id` is a syntactically valid account id — it does not compare it against the account being deleted. [3](#0-2) 

Crucially, the balance-refund receipt created in step 2 is a `Receipt::new_balance_refund`, which — per the protocol's refund model — is a `predecessor_id == "system"` receipt (free, no gas cost) that is executed *after* the deleting receipt's state changes are committed. If its `Transfer` fails because the target account (the just-deleted `beneficiary_id == account_id`) doesn't exist, the funds are **burned**, not re-refunded:

> "Refund receipts are identified by having `predecessor_id == "system"` ... If the execution of a refund fails, the refund amount is burnt." [4](#0-3) 

This is also documented at the implementation level: system-predecessor receipts that fail have their deposit added to `other_burnt_amount` rather than being refunded again. [5](#0-4) 

This exact "self-beneficiary" usage pattern is not a hypothetical edge case — it is the *default* helper behavior in the integration test harness: `delete_account(signer_id, receiver_id)` sets `beneficiary_id = signer_id`, and when a signer deletes its own account (`signer_id == receiver_id`, the common "close my own account" flow), `beneficiary_id` ends up equal to the account being deleted. [6](#0-5) 
This is exercised directly by `test_delete_account_ok`, which calls `delete_account(eve_dot_alice_account(), eve_dot_alice_account())` (self-delete, self-beneficiary) and asserts `SuccessValue` — the transaction reports success while the fate of the refunded balance (sent to the now-deleted account) is never verified. [7](#0-6) 

### Impact Explanation
For `NamedAccount`/sub-account IDs (the common case, e.g. `eve.alice.near`), a `Transfer` receipt targeting a non-existent account fails outright (no implicit-account auto-creation applies to non-implicit IDs), which per the refund semantics above causes the entire remaining account balance to be irrecoverably burned. Any wallet, SDK, CLI tool, or contract that lets a user delete their own account and pre-fills/accepts `beneficiary_id` equal to the account being closed (a very natural UX default — "return my balance to myself") will silently destroy the user's funds instead of returning them, with the transaction outcome reporting success. This is a concrete, permanent loss-of-funds bug reachable from an ordinary client transaction (`DeleteAccount` action), with no attacker required — matching the severity class of the referenced report (irreversible loss caused by a missing sanity check on a critical address-like parameter).

### Likelihood Explanation
High from a "user/tooling mistake" perspective: `DeleteAccountAction` is a standard action available to any signer with a full-access key on their own account, and self-beneficiary deletion is the exact pattern baked into the project's own test helper (`delete_account_with_beneficiary_set` default). No special privileges, races, or unusual conditions are required — only that a caller (human, wallet UI, or automated tooling) picks the account being deleted as its own beneficiary, which is a plausible and even encouraged default ("send remaining funds back to me").

### Recommendation
In `action_delete_account` (or in `validate_action`/`action_validation.rs` for `Action::DeleteAccount`), reject the action (return an `ActionError`, e.g. a new `DeleteAccountBeneficiaryIsSelf` kind) when `delete_account.beneficiary_id == account_id`, before any state mutation. This mirrors the mitigation from the referenced report: explicitly forbid a destination value that is provably a "dead end" (identical to the entity being destroyed) for a critical fund-routing parameter.

### Proof of Concept
1. Create sub-account `eve.alice.near` with an ED25519 full-access key and a non-zero balance (`TESTING_INIT_BALANCE / 2`), as done in `test_delete_account_ok`. [8](#0-7) 
2. Sign a `DeleteAccount` transaction from `eve.alice.near` to itself with `beneficiary_id = eve.alice.near` (exactly what `delete_account(eve_dot_alice_account(), eve_dot_alice_account())` produces via `delete_account_with_beneficiary_set`). [6](#0-5) 
3. Submit the transaction; observe `FinalExecutionStatus::SuccessValue` and that the account no longer exists (as asserted by the existing test). [9](#0-8) 
4. Inspect the produced `Receipt::new_balance_refund(&beneficiary_id, account_balance)` receipt's execution outcome: because `beneficiary_id` (`eve.alice.near`) no longer exists at the time this system-predecessor `Transfer` receipt executes, it fails and its deposit is added to `other_burnt_amount` per the refund-burn rule, rather than being credited to any live account — confirming permanent loss of the account's balance. [10](#0-9) [4](#0-3)

### Citations

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

**File:** runtime/runtime/src/action_validation.rs (L99-112)
```rust
    while let Some(action) = iter.next() {
        if let Action::DeleteAccount(_) = action {
            if iter.peek().is_some() {
                return Err(ActionsValidationError::DeleteActionMustBeFinal);
            }
        } else {
            if let Action::Delegate(_) | Action::DelegateV2(_) = action {
                if found_delegate_action {
                    return Err(ActionsValidationError::DelegateActionMustBeOnlyOne);
                }
                found_delegate_action = true;
            }
        }
        validate_action_with_mode(limit_config, action, receiver, current_protocol_version, mode)?;
```

**File:** docs/RuntimeSpec/Refunds.md (L10-12)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
```

**File:** protocol-model/spec/runtime-execution.md (L152-152)
```markdown
- **Refund receipts are free**: system-predecessor receipts burn zero gas; a failed refund burns its deposit into `other_burnt_amount` rather than refunding (`runtime/runtime/src/lib.rs:929`, `:972`).
```

**File:** integration-tests/src/user/mod.rs (L262-268)
```rust
    fn delete_account(
        &self,
        signer_id: AccountId,
        receiver_id: AccountId,
    ) -> Result<FinalExecutionOutcomeView, CommitError> {
        self.delete_account_with_beneficiary_set(signer_id.clone(), receiver_id, signer_id)
    }
```

**File:** integration-tests/src/tests/standard_cases/mod.rs (L1608-1621)
```rust
pub fn test_delete_account_ok(node: impl Node) {
    let money_used = TESTING_INIT_BALANCE.checked_div(2).unwrap();
    let node_user = node.user();
    let _ = node_user.create_account(
        alice_account(),
        eve_dot_alice_account(),
        node.signer().public_key(),
        money_used,
    );
    let transaction_result =
        node_user.delete_account(eve_dot_alice_account(), eve_dot_alice_account()).unwrap();
    assert_eq!(transaction_result.status, FinalExecutionStatus::SuccessValue(Vec::new()));
    assert!(node.user().view_account(&eve_dot_alice_account()).is_err());
}
```
