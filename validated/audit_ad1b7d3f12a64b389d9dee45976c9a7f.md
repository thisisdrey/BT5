### Title
`DeleteAccountAction` sends the deleted account's balance to an unvalidated `beneficiary_id`, permanently burning funds if it doesn't exist — ([File: runtime/runtime/src/actions.rs])

### Summary
The `DeleteAccountAction`'s `beneficiary_id` field is fully controlled by the account owner (via any signed transaction or contract-issued `promise_batch_action_delete_account`) and is never checked for existence before the account's remaining NEAR balance is dispatched to it. Because the resulting transfer is generated as a "refund" receipt, a failure due to a non-existent (e.g. typo'd) `beneficiary_id` results in the funds being permanently burned rather than returned to the sender — a direct analog of the reported bug, where an unvalidated destination address led to permanent fund loss.

### Finding Description
`action_delete_account` unconditionally builds a `Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance)` for the whole remaining balance with no validation that `beneficiary_id` refers to an existing account: [1](#0-0) 

This refund-style receipt is created with `predecessor_id = system`, which `apply_action` detects as `is_refund` and therefore marks the transfer as *not* eligible for implicit-account creation: [2](#0-1) 

`check_account_existence`/`check_transfer_to_nonexisting_account` reject a `Transfer` action to a nonexistent account unless it is eligible for implicit creation: [3](#0-2) 

Since refunds are explicitly excluded from implicit-account creation ("Refunds don't automatically create accounts, because refunds are free and we don't want some type of abuse" — comment at the same location), a `beneficiary_id` that does not correspond to any existing account (and isn't a valid implicit-account id being auto-created) causes the refund receipt itself to fail with `AccountDoesNotExist`.

Per the documented refund semantics and the runtime's handling of system-predecessor receipts, when a refund receipt's execution fails, its deposit is **burned**, not returned to anyone: [4](#0-3) [5](#0-4) 

So a single `DeleteAccount` action with a malformed/non-existent `beneficiary_id` (e.g., a typo in a named account, or an ETH/NEAR-implicit-looking string that doesn't actually decode/derive correctly) causes the entire remaining account balance to be irrecoverably destroyed. This mirrors the audited bug exactly: an address supplied without any existence/ownership check is used as the destination of an irreversible balance transfer, and failure of that transfer results in permanent loss of funds rather than a safe rollback/refund to the originator.

### Impact Explanation
This results in real, permanent loss of user funds (network-wide token burn), matching the "permanent freezing/loss of funds" bar. Any ordinary NEAR account holder (or any contract issuing `promise_batch_action_delete_account`, e.g. `runtime/near-vm-runner/src/wasmtime_runner/logic.rs:3884-3917`) can trigger this by supplying an incorrect `beneficiary_id`, destroying their entire account balance with no recourse — the same "manager sets wrong destination, no existence check, ETH stuck" outcome as the original finding.

### Likelihood Explanation
This is trivially reachable by any unprivileged signer through a standard `DeleteAccount` action or a contract that calls `promise_batch_action_delete_account` with a dynamically/incorrectly computed beneficiary id (e.g. concatenation bugs, wrong environment configuration, or contract logic errors). No special privileges, malicious nodes, or protocol-level control are required — only a normal transaction/receipt from the account owner or an access key holder.

### Recommendation
Before creating the balance-refund receipt in `action_delete_account`, validate that `beneficiary_id` corresponds to an existing account (or is a valid implicit-account identifier eligible for creation), analogous to the check already performed for ordinary `Transfer` actions via `check_transfer_to_nonexisting_account`. If the beneficiary does not exist, either reject the `DeleteAccount` action outright or fall back to a safe destination (e.g., the deleting account's predecessor) instead of allowing the balance to be silently burned on refund failure.

### Proof of Concept
1. Create an account `victim.near` holding a balance.
2. Submit a `DeleteAccountAction { beneficiary_id: "typo-nonexistent.near" }` where `typo-nonexistent.near` was never created (and is not a valid NEAR/ETH-implicit account id that would be auto-created).
3. `action_delete_account` computes `account_balance` and pushes `Receipt::new_balance_refund("typo-nonexistent.near", account_balance)` while deleting `victim.near`'s account state.
4. This refund receipt executes as a `Transfer` to a non-existent receiver; because `is_refund == true`, `implicit_account_creation_eligible` is `false`, so `check_transfer_to_nonexisting_account` returns `AccountDoesNotExist` and the receipt fails.
5. Per `runtime/runtime/src/lib.rs:993-1000`, since the failing receipt's predecessor is `system`, the entire deposit (the victim's former balance) is added to `stats.balance.other_burnt_amount` — i.e., permanently burned, not returned to `victim.near` or anyone else.

This chain is confirmed by code inspection; I was not able to execute an end-to-end integration test in this environment to observe the resulting `other_burnt_amount` value directly, but every step is backed by the cited source locations and the project's own documentation of refund-failure semantics.

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

**File:** runtime/runtime/src/actions.rs (L819-877)
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
        Action::DeterministicStateInit(_) => {
            // Existing and non existing is valid for DeterministicStateInit.
            // Does not exist => The account will be created by the action.
            // Does exist => Nothing happens but the receipt is not aborted to
            // allow optional init before other actions.
        }
        Action::DeployContract(_)
        | Action::FunctionCall(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeleteAccount(_)
        | Action::Delegate(_)
        | Action::DelegateV2(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::TransferToGasKey(_)
        | Action::WithdrawFromGasKey(_) => {
            if account.is_none() {
                return Err(ActionErrorKind::AccountDoesNotExist {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
    };
    Ok(())
}

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

**File:** runtime/runtime/src/lib.rs (L547-559)
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

**File:** docs/RuntimeSpec/Refunds.md (L10-13)
```markdown
Refund receipts are identified by having `predecessor_id == "system"`. They are also special because they don't cost any gas to generate or execute. As a result, they also do not contribute to the block gas limit.

If the execution of a refund fails, the refund amount is burnt.
The refund receipt is an `ActionReceipt` that consists of a single action `Transfer` with the `deposit` amount of the refund.
```
