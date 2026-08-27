### Title
`DeleteAccountAction` lets an unprivileged signer permanently burn their own account's balance via a single-step, unvalidated `beneficiary_id` - ([File: runtime/runtime/src/actions.rs])

### Summary
The external report describes a single-step role/ownership transfer (`MarketFactory.sol`'s `handOverHost`/`transferOwnership`) that only checks the target address is non-zero, but doesn't verify the address is reachable/correct, so a typo or malicious input permanently bricks fee collection. The nearcore analog is `DeleteAccountAction::beneficiary_id`: it is a single-step, one-shot designation of the recipient of an account's entire remaining balance, validated only for *syntactic* correctness (`InvalidAccountId`) — never for existence or reachability. If the signer (or anyone crafting the delete-account receipt, e.g. via a contract's batched promise) supplies a `beneficiary_id` that does not correspond to an existing, refund-eligible account, the resulting balance-refund receipt fails and the entire balance is unconditionally burned, with no recovery path.

### Finding Description
`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) takes the account's current balance and unconditionally queues a refund receipt to whatever `beneficiary_id` was supplied, without ever checking that the beneficiary account exists: [1](#0-0) 

This refund is delivered as an `ActionReceipt` with `predecessor_id == "system"` (a free "refund" receipt) via `Receipt::new_balance_refund`. When that receipt is later executed, `apply_action` computes `is_refund = receipt.predecessor_id().is_system()` and explicitly disables implicit-account creation for refunds: [2](#0-1) 

Because `implicit_account_creation_eligible` is `false` for refunds, even a syntactically-valid NEAR-implicit/ETH-implicit `beneficiary_id` that simply doesn't exist yet cannot be auto-created by this receipt; `check_transfer_to_nonexisting_account` unconditionally returns `AccountDoesNotExist`: [3](#0-2) 

When that refund receipt's execution fails, the runtime does not retry, bounce back to the original account, or otherwise recover the funds — it burns them outright: [4](#0-3) 

The only front-end validation of `beneficiary_id` is that it is a *well-formed* account id — never that it exists or is reachable, exactly mirroring the report's root cause ("basic validation checks whether the address is not a zero address... does not properly account for scenarios where the address receiving the role is inaccessible"): [5](#0-4) 

Unlike ordinary `Transfer` actions to a nonexistent account — which fail the whole transaction/receipt and simply leave the sender's balance untouched (see `test_refund_on_send_money_to_non_existent_account`) — the `DeleteAccount` beneficiary path has already destroyed the source account and irrevocably burns the balance once the beneficiary refund fails.

### Impact Explanation
This causes a permanent, protocol-level loss of NEAR balance for any account owner (or contract acting on its own behalf via a promise batch) that specifies an incorrect, non-existent, or not-yet-existing implicit `beneficiary_id` when deleting their account. There is no two-step "claim" mechanism and no fallback: the funds are burned (`stats.balance.other_burnt_amount`) rather than returned to the deleting account or held in escrow. This matches the class of "permanent freezing/loss of funds" called out as in-scope impact.

### Likelihood Explanation
This is trivially triggerable by any ordinary account holder issuing a `DeleteAccountAction` (directly via a signed transaction, or programmatically through a contract's `action_delete_account` promise batch, as shown in `test-loop-tests/src/tests/create_delete_account.rs`). No special privileges, race conditions, or malicious infrastructure are required — a simple typo in `beneficiary_id`, or specifying an implicit account id that has never been funded/created, is sufficient. It requires only ordinary transaction/contract capabilities available to any unprivileged signer, matching the required "unprivileged-signer" access class.

### Recommendation
Introduce validation (or a safer default) for `DeleteAccountAction::beneficiary_id` before destroying the source account:
- Require the beneficiary account to already exist (or be implicit-creatable in a way the refund path actually supports) at validation/execution time, failing the whole `DeleteAccount` action rather than silently burning tokens later, or
- Allow refund receipts targeting nonexistent implicit accounts to still trigger implicit-account creation (removing the `!is_refund` restriction specifically for `DeleteAccount` beneficiary refunds), or
- On beneficiary-refund failure, route the balance back to the original account's shard as a burn-avoiding fallback instead of unconditionally burning it.

### Proof of Concept
1. Account `alice.near` holds a balance and issues `DeleteAccountAction { beneficiary_id: "0x000...bad" }` (a syntactically valid ETH-implicit-style account id that has never received any deposit, i.e., does not exist), or any syntactically-valid but currently non-existent named account.
2. `action_delete_account` (`runtime/runtime/src/actions.rs:364-370`) removes `alice.near` and queues a `Receipt::new_balance_refund(beneficiary_id, account_balance)`.
3. That refund receipt executes with `predecessor_id == "system"`; `apply_action` sets `implicit_account_creation_eligible = false` because `is_refund == true` (`runtime/runtime/src/lib.rs:547-551`).
4. `check_account_existence` → `check_transfer_to_nonexisting_account` returns `AccountDoesNotExist` (`runtime/runtime/src/actions.rs:857-877`) since implicit creation is disallowed for refunds.
5. The refund receipt fails; per `runtime/runtime/src/lib.rs:993-1000`, the full `account_balance` is added to `other_burnt_amount` and permanently lost — `alice.near`'s funds are gone with no recovery. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

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

**File:** runtime/runtime/src/lib.rs (L547-551)
```rust
        let account_id = receipt.receiver_id();
        let is_refund = receipt.predecessor_id().is_system();
        let is_the_only_action = actions.len() == 1;
        let implicit_account_creation_eligible = is_the_only_action && !is_refund;

```

**File:** runtime/runtime/src/lib.rs (L993-1000)
```rust
        let gas_refund_result = if receipt.predecessor_id().is_system() {
            // If the refund fails tokens are burned.
            if result.result.is_err() {
                stats.balance.other_burnt_amount = safe_add_balance(
                    stats.balance.other_burnt_amount,
                    total_deposit(&action_receipt.actions())?,
                )?
            }
```

**File:** docs/RuntimeSpec/Actions.md (L278-300)
```markdown
## DeleteAccountAction

```rust
pub struct DeleteAccountAction {
    /// The remaining account balance will be transferred to the AccountId below
    pub beneficiary_id: AccountId,
}
```

**Outcomes**:

- The account, as well as all the data stored under the account, is deleted and the tokens are transferred to `beneficiary_id`.

### Errors

**Validation Error**:

- If `beneficiary_id` is not a valid account id, the following error will be returned

```rust
/// Invalid account ID.
InvalidAccountId { account_id: AccountId },
```
```
