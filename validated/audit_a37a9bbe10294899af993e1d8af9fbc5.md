Confirmed: comment at `runtime/runtime/src/actions.rs:869-872` explicitly documents that "refunds don't automatically create accounts... Account deletion with beneficiary creates a refund, so it'll not create a new account" [1](#0-0) . Combined with `check_account_existence` rejecting non-`Transfer` actions on nonexistent accounts and, for `Transfer`, rejecting nonexistent named (non-implicit) accounts outright [2](#0-1) , this validates the analog.

### Title
Self-beneficiary `DeleteAccount` permanently burns the account's balance instead of refunding it - (File: `runtime/runtime/src/actions.rs`)

### Summary
`action_delete_account` accepts any `beneficiary_id`, including the deleted account's own `account_id`, without any check that they differ. This mirrors the DoubleTokenLexscrow bug class where two identifiers that must be distinct are never validated as such, leading to permanent loss of the deposited/held funds.

### Finding Description
When a user submits `DeleteAccount { beneficiary_id }`, the runtime validates only that `beneficiary_id` is a syntactically valid account id — `validate_delete_action` calls `validate_action_account_id(&action.beneficiary_id)` and nothing else [3](#0-2) . There is no check that `beneficiary_id != account_id`.

`action_delete_account` then takes the account's remaining balance and schedules a *balance-refund receipt* addressed to `beneficiary_id`, before actually removing the account from state: [4](#0-3) 

This refund receipt has `predecessor_id == "system"` (it's built via `Receipt::new_balance_refund`, the same primitive used for deposit/gas refunds) and executes as a single `Transfer` action in a later, separate receipt-processing step [5](#0-4) .

If the caller sets `beneficiary_id == account_id`, the refund receipt targets the very account that was just deleted. By the time this refund receipt is processed, the account no longer exists in state. `check_account_existence` for a `Transfer` action on a nonexistent account only allows the transfer to proceed if the account id is *implicit* and the transfer is *eligible for implicit-account creation*; refunds are explicitly excluded from this eligibility, as documented in-line: "Refunds don't automatically create accounts, because refunds are free and we don't want some type of abuse. Account deletion with beneficiary creates a refund, so it'll not create a new account." [1](#0-0) . For an ordinary (non-implicit) named account — which is the common case for user accounts with a balance worth protecting — the refund transfer instead fails outright with `AccountDoesNotExist` [2](#0-1) [6](#0-5) .

Per the documented refund semantics, a refund receipt whose execution fails has its deposit **burned**, not retried or returned to any account: "If the execution of a refund fails, the refund amount is burnt" [7](#0-6) , and this is implemented at the point where a system-predecessor receipt's action fails: `stats.balance.other_burnt_amount` is incremented by the full deposit of the failed refund [8](#0-7) .

Net effect: an ordinary user, by simply naming their own (or another to-be-deleted, non-implicit) account as the `DeleteAccount` beneficiary, causes their entire remaining account balance to be irrecoverably burned instead of refunded — exactly the "two identifiers that should differ are never checked, causing stuck/lost funds" bug class from the external report, translated into nearcore's action-execution/refund path.

### Impact Explanation
This is a permanent, unrecoverable loss of user funds triggered entirely by an unprivileged signer's own transaction (no attacker or privileged actor needed — the victim self-inflicts, or a malicious dApp/relayer crafting the delete-account action for a meta-transaction victim could inflict it on them). The balance is not credited to any account; it is recorded as burnt, permanently removed from circulation. This matches the "permanent freezing/loss of funds" impact bar.

### Likelihood Explanation
Trivial to trigger: any account holder issuing a `DeleteAccount` action (directly, via a wallet UI bug, via a buggy dApp/contract that constructs the action programmatically, or via a NEP-366 meta-transaction where a relayer/dApp fills in `beneficiary_id` from user-controlled or mistaken input) with `beneficiary_id == account_id` hits this path deterministically. No race condition, no special permissions, no cross-shard timing dependency required. The `meta_tx_delete_account` test explicitly demonstrates the send-to-other-beneficiary success path but there is no test covering/rejecting the self-beneficiary case [9](#0-8) .

### Recommendation
Add a validation check — either in `validate_delete_action` (`runtime/runtime/src/action_validation.rs`) or in `action_delete_account` (`runtime/runtime/src/actions.rs`) — that rejects `DeleteAccountAction` when `beneficiary_id == account_id`, returning a dedicated `ActionErrorKind` (e.g., `DeleteAccountBeneficiaryEqualsAccount`). This prevents the burn-refund failure mode and requires a protocol-version gate since it changes previously-accepted transaction behavior.

### Proof of Concept
1. Create/fund account `A` with a nonzero balance.
2. Submit `SignedTransaction` with a single action: `Action::DeleteAccount(DeleteAccountAction { beneficiary_id: A })` (i.e., `beneficiary_id == account_id`, and `A` is a normal named/non-implicit account).
3. `action_delete_account` runs successfully, generating `Receipt::new_balance_refund(&A, account_balance)` and then removing account `A` from state [4](#0-3) .
4. The balance-refund receipt is later processed against `A`; since `A` no longer exists and is not eligible for implicit-account-creation via a refund, `check_account_existence` returns `AccountDoesNotExist` [2](#0-1) .
5. Because the receipt's `predecessor_id` is `"system"`, the failed refund's deposit is added to `other_burnt_amount` rather than refunded anywhere [8](#0-7) .
6. Result: the account's entire balance is permanently burned; no account receives it.

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

**File:** runtime/runtime/src/actions.rs (L819-827)
```rust
        Action::Transfer(_) => {
            if account.is_none() {
                return check_transfer_to_nonexisting_account(
                    config,
                    account_id,
                    implicit_account_creation_eligible,
                );
            }
        }
```

**File:** runtime/runtime/src/actions.rs (L844-852)
```rust
        | Action::TransferToGasKey(_)
        | Action::WithdrawFromGasKey(_) => {
            if account.is_none() {
                return Err(ActionErrorKind::AccountDoesNotExist {
                    account_id: account_id.clone(),
                }
                .into());
            }
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

**File:** runtime/runtime/src/action_validation.rs (L399-403)
```rust
fn validate_delete_action(action: &DeleteAccountAction) -> Result<(), ActionsValidationError> {
    validate_action_account_id(&action.beneficiary_id)?;

    Ok(())
}
```

**File:** core/primitives/src/receipt.rs (L493-497)
```rust
    /// Generates a receipt with a transfer from system for a given balance without a receipt_id.
    /// This should be used for token refunds instead of gas refunds.
    /// It doesn't refund the allowance of the access key. For gas refunds use `new_gas_refund`.
    pub fn new_balance_refund(receiver_id: &AccountId, refund: Balance) -> Self {
        Receipt::V0(ReceiptV0 {
```

**File:** docs/RuntimeSpec/Refunds.md (L10-13)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
The refund receipt is an `ActionReceipt` that consists of a single action `Transfer` with the `deposit` amount of the refund.
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

**File:** integration-tests/src/tests/features/delegate_action.rs (L574-619)
```rust
#[test]
fn meta_tx_delete_account() {
    let relayer = alice_account();
    let sender = eve_dot_alice_account();
    let receiver = sender.clone();
    let node = RuntimeNode::new(&relayer);

    // setup: create new account because the standard accounts are validators (can't be deleted)
    let balance = Balance::from_near(1);
    node.user()
        .create_account(
            relayer.clone(),
            sender.clone(),
            PublicKey::from_seed(KeyType::ED25519, sender.as_ref()),
            balance,
        )
        .expect("account setup failed")
        .assert_success();

    let fee_helper = fee_helper(&node);

    let actions =
        vec![Action::DeleteAccount(DeleteAccountAction { beneficiary_id: relayer.clone() })];

    // special case balance check for deleting account
    let gas_cost = fee_helper
        .prepaid_delete_account_cost()
        .checked_add(fee_helper.meta_tx_overhead_cost(&actions, &receiver))
        .unwrap();
    let (_tx_result, sender_diff, relayer_diff, receiver_diff) =
        check_meta_tx_execution(&node, actions, sender, relayer, receiver.clone());

    assert_eq!(
        sender_diff,
        -(balance.as_yoctonear() as i128),
        "sender should be deleted and thus have zero balance"
    );
    assert_eq!(sender_diff, receiver_diff);
    assert_eq!(
        relayer_diff,
        balance.as_yoctonear() as i128 - (gas_cost.as_yoctonear() as i128),
        "unexpected relayer balance"
    );
    let err = node.view_account(&receiver).expect_err("account should have been deleted");
    assert_eq!(err, "Account ID #eve.alice.near does not exist");
}
```
