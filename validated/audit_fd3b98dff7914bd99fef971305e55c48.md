Confirmed: `implicit_account_creation_eligible = is_the_only_action && !is_refund` [1](#0-0)  — since `Receipt::new_balance_refund` sets `predecessor_id: "system"`, `is_refund` is `true`, so this is *never* eligible for implicit account creation, regardless of the account-id format. Combined with `check_account_existence`'s `Transfer` branch calling `check_transfer_to_nonexisting_account`, which returns `AccountDoesNotExist` whenever `implicit_account_creation_eligible` is false [2](#0-1) [3](#0-2) , this refund receipt fails outright if `beneficiary_id` doesn't exist on-chain. And per `lib.rs`, a failed refund (predecessor is "system") just burns the deposit into `other_burnt_amount` instead of returning it anywhere [4](#0-3) .

### Title
`DeleteAccountAction.beneficiary_id` is never validated for existence, causing permanent token burn - (File: `runtime/runtime/src/actions.rs`)

### Summary
`action_delete_account` unconditionally deletes the calling account and routes its full balance to `beneficiary_id` via a `Receipt::new_balance_refund` (a "system"-predecessor refund receipt), without ever checking that `beneficiary_id` refers to an existing (or creatable) account.

### Finding Description
When a `DeleteAccountAction` is executed, `action_delete_account` computes `account_balance` and, if nonzero, pushes `Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance)` before deleting the account [5](#0-4) . `Receipt::new_balance_refund` builds a receipt whose `predecessor_id` is the literal `"system"` account and whose single action is a plain `Transfer` [6](#0-5) .

The only validation ever performed on `beneficiary_id` is a *syntax* check (`InvalidAccountId`) at transaction-validation time, documented in `docs/RuntimeSpec/Actions.md` [7](#0-6)  — there is no check that the account actually exists, is spelled correctly, or is capable of receiving funds.

When this refund receipt is later applied, `apply_action` computes `is_refund = receipt.predecessor_id().is_system()` (true here) and `implicit_account_creation_eligible = is_the_only_action && !is_refund`, which is always `false` for refunds [1](#0-0) . `check_account_existence`'s `Transfer` branch then calls `check_transfer_to_nonexisting_account`, which — because `implicit_account_creation_eligible` is `false` — unconditionally returns `ActionErrorKind::AccountDoesNotExist` if the beneficiary account is not already present in state [2](#0-1) [3](#0-2) . The code comment explicitly states: *"Refunds don't automatically create accounts... Account deletion with beneficiary creates a refund, so it'll not create a new account."*

Because the failing receipt's `predecessor_id` is `"system"`, the runtime's refund-of-refund logic treats this as unrecoverable and burns the deposit instead of generating any further refund: `if receipt.predecessor_id().is_system() { if result.result.is_err() { stats.balance.other_burnt_amount = safe_add_balance(...) } }` [4](#0-3) . There is no bounce-back to the original account owner or the account that issued `DeleteAccount`.

### Impact Explanation
Any ordinary account owner who submits `Action::DeleteAccount(DeleteAccountAction { beneficiary_id })` with a `beneficiary_id` that does not currently exist — due to a typo, a beneficiary account that was itself deleted/never created, or simply picking an address nobody controls — irrecoverably burns their entire remaining account balance. This is a direct analog of the reported bug class: a value-moving operation lacks a destination-existence/zero-address-style check, and once executed the position (here, the account's NEAR balance) is permanently and irreversibly lost with no way to recover it, matching "permanent loss of funds."

### Likelihood Explanation
This is trivially reachable by any signer: a single `DeleteAccountAction` from an ordinary transaction, no privileged role or special network condition needed. The only requirement is that the operator picks (by mistake, phishing, or bad tooling) a `beneficiary_id` that is not an already-existing account and does not otherwise qualify for implicit account creation (e.g., a named account, or an implicit-looking account combined with the disqualifying `is_refund` flag). This makes user error the sole precondition, similar to the original finding's "if called by error with this value."

### Recommendation
Before generating the balance-refund receipt in `action_delete_account`, verify that `beneficiary_id` already exists in state (or, alternatively, explicitly allow implicit-account creation for the delete-account beneficiary refund path rather than closing that path off entirely). If the beneficiary account does not exist, reject the `DeleteAccountAction` with an execution/validation error instead of silently deleting the source account and burning its balance.

### Proof of Concept
1. Create account `alice.near` with a nonzero balance.
2. Sign and submit `SignedTransaction::from_actions(..., vec![Action::DeleteAccount(DeleteAccountAction { beneficiary_id: "nonexistent-account.near".parse().unwrap() })], ...)`, mirroring the test helper pattern in `integration-tests/src/tests/features/delegate_action.rs` [8](#0-7) , but using a `beneficiary_id` that has never been created on-chain.
3. Observe: `alice.near`'s account is deleted (`remove_account` succeeds unconditionally) [5](#0-4) ; the follow-up `Receipt::new_balance_refund` to `nonexistent-account.near` fails with `AccountDoesNotExist`; and because it's a system/refund receipt, the balance is added to `other_burnt_amount` rather than returned to anyone — the funds are permanently lost.

### Citations

**File:** runtime/runtime/src/lib.rs (L548-550)
```rust
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

**File:** runtime/runtime/src/actions.rs (L819-826)
```rust
        Action::Transfer(_) => {
            if account.is_none() {
                return check_transfer_to_nonexisting_account(
                    config,
                    account_id,
                    implicit_account_creation_eligible,
                );
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

**File:** docs/RuntimeSpec/Actions.md (L291-300)
```markdown
### Errors

**Validation Error**:

- If `beneficiary_id` is not a valid account id, the following error will be returned

```rust
/// Invalid account ID.
InvalidAccountId { account_id: AccountId },
```
```

**File:** integration-tests/src/tests/features/delegate_action.rs (L595-596)
```rust
    let actions =
        vec![Action::DeleteAccount(DeleteAccountAction { beneficiary_id: relayer.clone() })];
```
