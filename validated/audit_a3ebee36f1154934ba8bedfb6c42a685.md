This confirms the vulnerability. `remove_account` at [1](#0-0)  only removes `TrieKey::Account`, `TrieKey::ContractCode`, all access keys/gas-key nonces, and `TrieKey::ContractData` — it never touches `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceipt`. `action_delete_account` at [2](#0-1)  calls `remove_account` and then sets `*account = None`, but does not perform any postponed-receipt cleanup either. Meanwhile `process_receipt`'s Data-receipt branch, when a `DataReceipt` arrives, looks up `TrieKey::PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` purely keyed by `(receiver_id, ...)` with **no account-existence check at all**, and executes the postponed receipt once the count hits zero, as seen in [3](#0-2) . `check_actor_permissions` for owner-only actions only checks `actor_id != account_id` [4](#0-3) , and `actor_id` is set from `receipt.predecessor_id()`, so a self-issued postponed receipt (predecessor_id == receiver_id == account_id) always satisfies this check regardless of which "incarnation" of the account is currently live.

### Title
Postponed-receipt state (`PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt`) survives `DeleteAccount`, allowing authorization escalation against a re-created account - (File: `core/store/src/utils/mod.rs`, `runtime/runtime/src/actions.rs`)

### Summary
`remove_account` (`core/store/src/utils/mod.rs:505`), invoked by `action_delete_account` (`runtime/runtime/src/actions.rs:314`), deletes the `Account`, access keys, contract code, and contract data, but leaves any `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` trie entries keyed by that `receiver_id` untouched. If the account is deleted while a self-issued postponed receipt (created via two outgoing `promise_batch_then` receipts to itself) is still awaiting its data dependencies, and the account name is then recreated via `CreateAccount` before the outstanding `DataReceipt` arrives, the stale postponed receipt is later "resurrected" and executed against the brand-new account.

### Finding Description
An attacker deploys a contract to `sub.alice.near` and, in a single `FunctionCall` receipt, schedules two async `promise_batch_then` receipts targeting itself, producing an `ActionReceipt` with two `input_data_ids`. Since neither `DataReceipt` has arrived yet, `process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`) stores `PendingDataCount = 2` and the receipt itself under `PostponedReceipt{receiver_id: sub.alice.near, receipt_id}`, plus two `PostponedReceiptId` entries.

Before the second `DataReceipt` arrives, the attacker submits a `DeleteAccount` action (as owner, satisfying `check_actor_permissions`) with `beneficiary_id = alice.near`. `action_delete_account` calls `remove_account`, which deletes only `Account`/`ContractCode`/access keys/gas keys/`ContractData` — it never issues `state_update.remove` for `PostponedReceiptId`, `PendingDataCount`, or `PostponedReceipt`. Those three trie entries, still keyed by `receiver_id = sub.alice.near`, remain live.

The attacker (or anyone) then submits `CreateAccount` for `sub.alice.near` again. `check_account_existence` (`runtime/runtime/src/actions.rs:787-818`) only checks `TrieKey::Account`, which is now absent, so the create succeeds and installs a fresh key/owner — unaware that stale postponed-receipt bookkeeping for the same account name still exists.

When the second (still in-flight) `DataReceipt` for the original `receipt_id` finally arrives, `process_receipt`'s Data branch (`runtime/runtime/src/lib.rs:1396-1455`) looks up `PostponedReceiptId{receiver_id: sub.alice.near, data_id}` — found — decrements `PendingDataCount` from 1 to 0, fetches the postponed `Receipt` via `get_postponed_receipt`, and calls `apply_action_receipt` on it. Nothing in this path checks whether the account that currently exists at `sub.alice.near` is the same account that was alive when the receipt was postponed. Because the postponed receipt's actions were self-targeted (predecessor_id == receiver_id == `sub.alice.near`), `check_actor_permissions` for `AddKey`/`DeployContract`/`Stake` passes trivially (`actor_id == account_id`), so the pre-deletion contract's chosen owner-only actions execute against the *new* account, without any consent from the new account's actual owner.

### Impact Explanation
This is an authorization-escalation bug: an attacker who controls the *old* contract logic on `sub.alice.near` can pre-plan an `AddKey`/`DeployContract`/`Stake` action to be delivered later, delete the account (refunding themselves via `beneficiary_id`), let someone else (or the same attacker publicly) recreate `sub.alice.near`, and then have the resurrected postponed receipt silently inject a full-access key, deploy arbitrary code, or stake on behalf of the new account holder — none of which the new owner authorized. This matches the "authorization escalation across accounts or promises" bounty category and can lead to fund theft/freezing on the newly created account (e.g., attacker adds a full-access key to the new account and drains it).

### Likelihood Explanation
Preconditions are fully within an unprivileged attacker's control: fund and control `sub.alice.near`, deploy an arbitrary contract, and schedule the async two-promise pattern. The only external dependency is timing — the second `DataReceipt` must still be "in flight" (cross-shard/cross-chunk delay) when `DeleteAccount` and `CreateAccount` execute; this delay is a normal artifact of the receipt-matching/postponed-receipt protocol design (cross-shard congestion, multi-block resolution) and is trivially reproducible in a controlled unit/integration test by simply not delivering the second `DataReceipt` before issuing `DeleteAccount`+`CreateAccount`. No validator/node privilege or race against consensus is required; an unprivileged actor fully controls transaction ordering within the constraint that account-name reuse and postponed-receipt resolution are asynchronous by protocol design.

### Recommendation
`remove_account` (or `action_delete_account`) should also enumerate and remove any `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` entries keyed by the deleted `receiver_id` (mirroring how access keys and contract data are enumerated via trie prefix iteration), refunding/erroring appropriately for any still-pending cross-account promises. Alternatively/additionally, `DeleteAccount` should be disallowed (or the pending receipts should be forcibly resolved/dropped with an error) whenever outstanding `PendingDataCount` entries exist for the account, and `CreateAccount` (or receipt delivery in `process_receipt`) should refuse to resurrect/execute a postponed receipt if the account existed, was deleted, and recreated in between (e.g., by tracking a monotonic "account incarnation" and validating recorded incarnation in the postponed-receipt data before execution).

### Proof of Concept
Integration/`apply()`-level test plan:
1. Fund `alice.near`; create sub-account `sub.alice.near` and deploy a test contract capable of issuing `promise_batch_create`/`promise_batch_then`/`promise_batch_action_add_key` (or `action_stake`/`action_deploy_contract`) with two outgoing promises targeting itself.
2. Submit a `FunctionCall` receipt to `sub.alice.near` that creates two `promise_batch_then` receipts back to itself, each producing one `DataReceipt` back to the caller — resulting in a postponed `ActionReceipt` with `PendingDataCount = 2` for `sub.alice.near`, where the postponed receipt's action batch includes `AddKey(attacker_key)`.
3. Before delivering the second `DataReceipt`, apply a `DeleteAccount` receipt for `sub.alice.near` (beneficiary `alice.near`) in the same or a subsequent chunk; assert via direct trie lookups (`get_postponed_receipt`, `get`/`TrieKey::PendingDataCount`) that the postponed-receipt state still exists after `Account` is removed.
4. Apply a `CreateAccount` receipt for `sub.alice.near` with a fresh key; assert it succeeds (no `AccountAlreadyExists`).
5. Deliver the still-pending second `DataReceipt` for the original `receipt_id`; assert (a) the postponed receipt executes without error, and (b) `attacker_key` (or the attacker-deployed contract/stake) is now present on the *new* `sub.alice.near` account, despite that account's real owner never authorizing it — proving state mutation across the delete/recreate boundary.

### Citations

**File:** core/store/src/utils/mod.rs (L505-575)
```rust
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
    let mut gas_key_nonce_total_key_bytes: usize = 0;

    // Removing access keys and gas key nonces
    let lock = state_update.trie().lock_for_iter();
    let mut keys_to_remove: Vec<TrieKey> = Vec::new();
    for raw_key in state_update
        .locked_iter(&trie_key_parsers::get_raw_prefix_for_access_keys(account_id), &lock)?
    {
        let raw_key = raw_key?;
        let key_handle = trie_key_parsers::parse_key_handle_from_access_key_key(
            &raw_key, account_id,
        )
        .map_err(|_e| {
            StorageError::StorageInconsistentState(
                "Can't parse key handle from raw key for AccessKey".to_string(),
            )
        })?;
        let nonce_index =
            trie_key_parsers::parse_nonce_index_from_gas_key_key(&raw_key, account_id, &key_handle)
                .map_err(|_e| {
                    StorageError::StorageInconsistentState(
                        "Can't parse nonce index from raw key for AccessKey".to_string(),
                    )
                })?;
        if let Some(index) = nonce_index {
            gas_key_nonce_count += 1;
            gas_key_nonce_total_key_bytes += raw_key.len();
            keys_to_remove.push(TrieKey::gas_key_nonce(
                account_id.clone(),
                key_handle.clone(),
                index,
            ));
        } else {
            keys_to_remove.push(TrieKey::access_key(account_id.clone(), key_handle.clone()));
        }
    }
    drop(lock);

    for trie_key in keys_to_remove {
        state_update.remove(trie_key);
    }

    // Removing contract data
    let lock = state_update.trie().lock_for_iter();
    let data_keys = state_update
        .locked_iter(&trie_key_parsers::get_raw_prefix_for_contract_data(account_id, &[]), &lock)?
        .map(|raw_key| {
            trie_key_parsers::parse_data_key_from_contract_data_key(&raw_key?, account_id)
                .map_err(|_e| {
                    StorageError::StorageInconsistentState(
                        "Can't parse data key from raw key for ContractData".to_string(),
                    )
                })
                .map(Vec::from)
        })
        .collect::<Result<Vec<_>, _>>()?;
    drop(lock);

    for key in data_keys {
        state_update.remove(TrieKey::ContractData { account_id: account_id.clone(), key });
    }
    Ok(RemoveAccountResult { gas_key_nonce_count, gas_key_nonce_total_key_bytes })
}
```

**File:** runtime/runtime/src/actions.rs (L364-389)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
    result.tokens_burnt =
        result.tokens_burnt.checked_add(gas_key_balance_to_burn).ok_or_else(|| {
            StorageError::StorageInconsistentState("tokens_burnt overflow".to_string())
        })?;
    if remove_result.gas_key_nonce_count > 0 {
        let compute = storage_removes_compute(
            &config.wasm_config.ext_costs,
            remove_result.gas_key_nonce_count,
            remove_result.gas_key_nonce_total_key_bytes,
            AccessKey::NONCE_VALUE_LEN * remove_result.gas_key_nonce_count,
        );
        result.compute_usage = safe_add_compute(result.compute_usage, compute).map_err(|_| {
            StorageError::StorageInconsistentState("compute_usage overflow".to_string())
        })?;
    }
    *actor_id = receipt.predecessor_id().clone();
    *account = None;
    Ok(())
```

**File:** runtime/runtime/src/actions.rs (L739-776)
```rust
pub(crate) fn check_actor_permissions(
    action: &Action,
    account: &Option<Account>,
    actor_id: &AccountId,
    account_id: &AccountId,
) -> Result<(), ActionError> {
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
        Action::DeleteAccount(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
            let account = account.as_ref().unwrap();
            if !account.locked().is_zero() {
                return Err(ActionErrorKind::DeleteAccountStaking {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
```

**File:** runtime/runtime/src/lib.rs (L1396-1455)
```rust
                // given data_id.
                // If we don't have a postponed receipt yet, we don't need to do anything for now.
                if let Some(receipt_id) = get(
                    state_update,
                    &TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    },
                )? {
                    // There is already a receipt that is awaiting for the just received data.
                    // Removing this pending data_id for the receipt from the state.
                    state_update.remove(TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    });
                    // Checking how many input data items is pending for the receipt.
                    let pending_data_count: u32 = get(
                        state_update,
                        &TrieKey::PendingDataCount { receiver_id: account_id.clone(), receipt_id },
                    )?
                    .ok_or_else(|| {
                        StorageError::StorageInconsistentState(
                            "pending data count should be in the state".to_string(),
                        )
                    })?;
                    if pending_data_count == 1 {
                        // It was the last input data pending for this receipt. We'll cleanup
                        // some receipt related fields from the state and execute the receipt.

                        // Removing pending data count from the state.
                        state_update.remove(TrieKey::PendingDataCount {
                            receiver_id: account_id.clone(),
                            receipt_id,
                        });
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
