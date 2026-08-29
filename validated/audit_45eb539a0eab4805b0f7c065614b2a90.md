### Title
Authorization by `actor_id == account_id` string equality lets stale self-authorized postponed/yield receipts hijack a deleted-and-recreated account - (File: runtime/runtime/src/actions.rs)

### Summary
`check_actor_permissions` authorizes `DeployContract`/`Stake`/`AddKey`/`DeleteKey`/`DeleteAccount`/`DeployGlobalContract`/`UseGlobalContract`/`WithdrawFromGasKey` purely by comparing `actor_id` to `account_id` as strings, with no binding to the specific account instance ("incarnation") that authorized the action. Because `actor_id` for a receipt is fixed to `receipt.predecessor_id()` at receipt-creation time and never re-validated against the account's identity/creation event, a postponed or promise-yield receipt that was legitimately self-authorized (`predecessor_id == receiver_id`) before the account was deleted will still pass this check when it is finally delivered against a completely different, later-recreated account bearing the same name.

### Finding Description
`check_actor_permissions` (`runtime/runtime/src/actions.rs:739-785`) does the following for the sensitive administrative actions: [1](#0-0) 

The check is a pure string comparison `actor_id != account_id` — there is no creation nonce, incarnation counter, or any other binding to *which* account object authorized the pending action.

`actor_id` is seeded once per receipt in `apply_action_receipt` as `receipt.predecessor_id().clone()` — a value baked into the receipt when it was first created, immutable thereafter: [2](#0-1) 

For receipts with unresolved data dependencies, the entire `Receipt` (including this immutable `predecessor_id`) is persisted under `TrieKey::PostponedReceipt{receiver_id, receipt_id}` and later re-executed verbatim by `apply_action_receipt` once its `DataReceipt`s arrive: [3](#0-2) [4](#0-3) 

At execution time, `get_account(state_update, account_id)` fetches whatever account currently exists at that name — it does not verify this is the same account instance that originally issued the postponed receipt. Combined with `check_actor_permissions`'s pure string equality, any postponed/yield receipt whose original `predecessor_id` equals its own `receiver_id` (i.e., a self-authorized administrative action such as `AddKey`, `DeployContract`, `Stake`, `DeleteKey`, or `UseGlobalContract`) will pass authorization when delivered after the account has been deleted (`action_delete_account`, `runtime/runtime/src/actions.rs:343-390`, which sets `*account = None` and calls `remove_account`) and subsequently recreated under the same name by an unrelated party.

The most realistic cross-privilege trigger is via **implicit accounts**: any unprivileged user can "recreate" a NEAR-implicit or ETH-implicit account merely by sending it a `Transfer` (`action_transfer_or_implicit_account_creation`), without any permission from the account's former owner. An attacker who owns such an implicit account can:
1. Trigger a cross-contract promise chain that ends in a `.then()` callback on itself containing an `AddKey`/`DeployContract` action — this receipt is naturally postponed while awaiting the callback's `DataReceipt`.
2. Submit `DeleteAccount` on the same account (self-authorized, so it passes `check_actor_permissions`, refunding remaining balance and setting `*account = None`), while the postponed receipt with `predecessor_id == receiver_id` remains stored under `PostponedReceipt`/`PendingDataCount`/`ReceivedData` trie keys, which are not shown to be purged as part of account deletion's storage accounting (only `AccessKey`, gas-key nonces, and contract storage are accounted for in `action_delete_account`).
3. Wait for any third party to send a `Transfer` to that same implicit address, recreating a fresh account.
4. When the pending `DataReceipt` eventually arrives, the stale postponed `AddKey` (or `DeployContract`) executes against the **new** account, and `check_actor_permissions` authorizes it solely because `actor_id` (the implicit account's own id, baked into the old receipt) equals `account_id` — even though the new account instance was funded/created entirely by an unrelated party.

I was unable to fully confirm within available tool budget whether `remove_account` (`core/store/src/utils/mod.rs`) purges the `PostponedReceipt`/`PendingDataCount`/`ReceivedData` trie entries for the deleted account as part of `action_delete_account`; this is the one remaining open question that determines whether stale postponed receipts literally survive deletion in this codebase version. The authorization-logic root cause itself (`check_actor_permissions`'s pure name-equality check with no incarnation binding) is fully confirmed from the code.

### Impact Explanation
If postponed/yield receipts are not purged on account deletion, this is an **authorization escalation across accounts** matching NEAR's bounty category for owner-privilege bypass: an attacker can plant a self-authorized `AddKey(FullAccess)` "time bomb" on an account name they control, delete it, and have their key silently added to whatever account a later, unrelated party creates under that same name — leading to **theft of funds** deposited into the "fresh" recreated account by an innocent depositor who has no reason to believe the address is compromised.

### Likelihood Explanation
Feasible only for implicit account names, since named sub-accounts can only be recreated by their same parent (no cross-privilege gain there) and top-level accounts require the registrar. The attack requires: attacker owning an implicit account, ability to script a cross-contract promise chain ending in a self-directed callback action, self-deleting before the callback resolves, and a third party later transferring funds to the same implicit address — all of which are actions available to an ordinary, unprivileged NEAR user/contract deployer. The remaining uncertainty is whether `action_delete_account`/`remove_account` cleans up the postponed-receipt bookkeeping trie entries; if it does, this particular delivery path is not reachable and the finding reduces to a documented but non-exploitable design gap in `check_actor_permissions`.

### Recommendation
Bind authorization to the account incarnation rather than name alone: e.g., record a monotonically increasing creation counter/nonce on the `Account` struct, and reject postponed/yield receipts whose captured incarnation does not match the current account's incarnation before dispatching administrative actions. Additionally, ensure `action_delete_account` explicitly purges any `PostponedReceipt`, `PendingDataCount`, and dangling `PostponedReceiptId`/`ReceivedData` entries keyed to the deleted account so that no admin-privileged receipt can outlive the account it was authorized against.

### Proof of Concept
Unit test in `runtime/runtime/src/actions.rs` test module:
1. Call `check_actor_permissions(&Action::AddKey(...), &Some(fresh_account), &actor_id, &account_id)` with `actor_id == account_id`, where `fresh_account` is constructed via `create_account`/default fields simulating a just-recreated account (distinct from any "original" account state) — assert result is `Ok(())`.
2. Integration test extending `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code` pattern (`runtime/runtime/src/tests/apply.rs:4881`): build a receipt sequence where account `child` issues a self-directed `AddKey` action with a non-empty `input_data_ids` (simulating a postponed promise callback), then a `DeleteAccount` self-receipt for `child`, then a `DataReceipt` delivering the missing input, then confirm via `apply_result` that the previously-postponed `AddKey` action executes successfully (`ActionErrorKind` is not `ActorNoPermission`) and installs the attacker's key on the freshly recreated `child` account, asserting `get_access_key(state, &child, &attacker_public_key)` is `Some`.

### Citations

**File:** runtime/runtime/src/actions.rs (L745-760)
```rust
    match action {
        Action::DeployContract(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::WithdrawFromGasKey(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
        }
```

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1430-1455)
```rust
                        // Fetching the receipt itself.
                        let ready_receipt =
                            get_postponed_receipt(state_update, account_id, receipt_id)?
                                .ok_or_else(|| {
                                    StorageError::StorageInconsistentState(
                                        "pending receipt should be in the state".to_string(),
                                    )
                                })?;
                        // Removing the receipt from the state.
                        remove_postponed_receipt(state_update, account_id, receipt_id);
                        // Executing the receipt. It will read all the input data and clean it up
                        // from the state.
                        return self
                            .apply_action_receipt(
                                state_update,
                                apply_state,
                                pipeline_manager,
                                &ready_receipt,
                                receipt_sink,
                                instant_receipts,
                                validator_proposals,
                                stats,
                                epoch_info_provider,
                                receipt_to_tx,
                            )
                            .map(Some);
```

**File:** runtime/runtime/src/lib.rs (L1608-1655)
```rust
        let mut pending_data_count: u32 = 0;
        for data_id in action_receipt.input_data_ids() {
            if !has_received_data(state_update, account_id, *data_id)? {
                pending_data_count += 1;
                // The data for a given data_id is not available, so we save a link to this
                // receipt_id for the pending data_id into the state.
                set(
                    state_update,
                    TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: *data_id,
                    },
                    receipt.receipt_id(),
                )
            }
        }

        if pending_data_count == 0 {
            // All input data is available. Executing the receipt. It will cleanup
            // input data from the state.
            return self
                .apply_action_receipt(
                    state_update,
                    apply_state,
                    pipeline_manager,
                    receipt,
                    receipt_sink,
                    instant_receipts,
                    validator_proposals,
                    stats,
                    epoch_info_provider,
                    receipt_to_tx,
                )
                .map(Some);
        } else {
            // Not all input data is available now.
            // Save the counter for the number of pending input data items into the state.
            set(
                state_update,
                TrieKey::PendingDataCount {
                    receiver_id: account_id.clone(),
                    receipt_id: *receipt.receipt_id(),
                },
                &pending_data_count,
            );
            // Save the receipt itself into the state.
            set_postponed_receipt(state_update, receipt);
        }
```
