### Title
Insufficient validation of `DeleteAccountAction.beneficiary_id` causes permanent burn of account balance - (File: `runtime/runtime/src/action_validation.rs`)

### Summary
`DeleteAccountAction` only validates that `beneficiary_id` is a syntactically well-formed account id, never that it refers to an existing (or even creatable) account. Because the resulting balance transfer is generated as a "refund" receipt, and refund receipts that fail execution have their deposit permanently burned rather than retried or returned, a signer can unintentionally (or a relayer/dApp could be tricked into) burn the entire remaining balance of a deleted account by supplying a `beneficiary_id` that does not exist.

### Finding Description
`validate_delete_action` performs only a format check on the beneficiary account id, with no existence or reachability check: [1](#0-0) 

When `DeleteAccount` executes, the account's remaining balance is packaged into a "balance refund" receipt addressed to `beneficiary_id`, without checking whether that account exists: [2](#0-1) 

`Receipt::new_balance_refund` marks the receipt as a system-refund receipt (`predecessor_id = "system"`): [3](#0-2) 

When this refund receipt is later processed as a `Transfer` action against a non-existent receiver, `check_account_existence` rejects it with `AccountDoesNotExist` unless the receiver is eligible for implicit account creation — and refund receipts are explicitly excluded from implicit-account creation ("Refunds don't automatically create accounts... Account deletion with beneficiary creates a refund, so it'll not create a new account."): [4](#0-3) 

Finally, when a system-predecessor (refund) receipt fails, its deposit is unconditionally burned instead of being returned to the original account or retried: [5](#0-4) 

This is architecturally identical to the reported Solidity bug class: two logically coupled parameters (`escrowPortion`/`escrowPool`, here "non-zero balance to transfer"/"beneficiary account must exist") are validated independently rather than jointly, so a combination that passes both individual checks (valid-looking account id + non-zero balance) leads to an unrecoverable loss of funds. The docs for `Refunds.md` and `DeleteAccountAction` even hint at this by stating beneficiary_id is validated only for id-format correctness, not existence, and separately that "If the execution of a refund fails, the refund amount is burnt." [6](#0-5) 

### Impact Explanation
Any ordinary transaction signer (an account owner deleting their own account, or a `FunctionCall` contract issuing `promise_batch_action_delete_account` on behalf of a user, e.g. lockup/vesting contracts, wallets, or relayers) can cause the entire remaining NEAR balance of the deleted account to be permanently and irrecoverably burned if the specified `beneficiary_id` does not exist at the time the refund receipt executes (e.g., a typo, a beneficiary account deleted/never created, or a race where the beneficiary account is deleted between construction and execution of the delete transaction). This is a genuine, permanent loss of user funds with no recovery path — the funds are burned, not refunded to signer/predecessor.

### Likelihood Explanation
Likelihood is moderate: this requires a mistaken or malicious `beneficiary_id` (non-existent account) in an otherwise ordinary, unprivileged `DeleteAccount` action or promise batch action, which is directly reachable by any signer or by any deployed contract via the `near-vm-runner` host function `promise_batch_action_delete_account`. No special privileges are required, and the failure mode (burn, not refund/rollback) is systemic and always triggers whenever the beneficiary does not exist, not merely a low-probability edge case.

### Recommendation
Before generating the balance-refund receipt in `action_delete_account`, verify that `beneficiary_id` corresponds to an existing account (or, at minimum, reject deletion with a validation/execution error such as `BeneficiaryDoesNotExist` if this cannot cheaply be verified synchronously due to cross-shard/state design). Alternatively, change the "refund fails → burn" semantics specifically for the delete-account beneficiary payout so failed beneficiary transfers are returned to `actor_id`/predecessor instead of burned, closing the parallel to "if escrowPortion != 0 then escrowPool must not be zero."

### Proof of Concept
1. Alice creates account `carol.alice.near` with a nonzero balance.
2. Alice never creates `nonexistent.alice.near` (a syntactically valid sub-account id).
3. Alice signs a transaction: `Action::DeleteAccount(DeleteAccountAction { beneficiary_id: "nonexistent.alice.near" })` on `carol.alice.near`.
4. `validate_delete_action` passes (format-only check).
5. `action_delete_account` deletes `carol.alice.near` and enqueues `Receipt::new_balance_refund("nonexistent.alice.near", account_balance)`.
6. The refund receipt executes as `Transfer` against `nonexistent.alice.near`; `check_account_existence` returns `AccountDoesNotExist` because `implicit_account_creation_eligible` is false for refund receipts (see `runtime/runtime/src/actions.rs:857-877`).
7. Because `receipt.predecessor_id().is_system()` is true and `result.result.is_err()`, the deposit (the entire deleted account's balance) is added to `stats.balance.other_burnt_amount` and permanently lost (`runtime/runtime/src/lib.rs:993-1001`).

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

**File:** core/primitives/src/receipt.rs (L493-510)
```rust
    /// Generates a receipt with a transfer from system for a given balance without a receipt_id.
    /// This should be used for token refunds instead of gas refunds.
    /// It doesn't refund the allowance of the access key. For gas refunds use `new_gas_refund`.
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
