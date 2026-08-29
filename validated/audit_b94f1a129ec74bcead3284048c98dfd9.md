### Title
Stale PostponedReceipt / PendingDataCount / PostponedReceiptId trie entries survive DeleteAccount, enabling privilege escalation on account re-creation - (File: core/store/src/utils/mod.rs, runtime/runtime/src/lib.rs)

### Finding Description
`remove_account` in `core/store/src/utils/mod.rs` (lines 505-575) removes only `TrieKey::Account`, `TrieKey::ContractCode`, access keys, gas key nonces, and `TrieKey::ContractData` for the deleted account: [1](#0-0) [2](#0-1) 

It never iterates or removes `TrieKey::PendingDataCount`, `TrieKey::PostponedReceiptId`, or `TrieKey::PostponedReceipt` entries keyed by `receiver_id`/`receipt_id`/`data_id`. These keys are written when a multi-`input_data_ids` `ActionReceipt` is postponed awaiting data dependencies, and are only cleaned up along the data-arrival path in `apply_data_receipt` (`runtime/runtime/src/lib.rs`): [3](#0-2) 

When the second data dependency finally arrives and `pending_data_count` reaches 0, the code fetches and executes the previously postponed `Action` receipt via `apply_action_receipt` without re-validating that the receiver account still corresponds to the same logical entity that originally scheduled the callback: [4](#0-3) 

Exploit flow: an attacker submits a self-targeted multi-dependency callback (`promise.and(...)`-style) to their own account, satisfies one dependency (decrementing `PendingDataCount` from 2 to 1 and clearing one `PostponedReceiptId`), then issues `DeleteAccount`. `remove_account` deletes the `Account`/`ContractCode`/keys/`ContractData` records but leaves `PendingDataCount=1`, the remaining `PostponedReceiptId`, and the `PostponedReceipt` itself in the trie under the same account name. The attacker recreates the account (`CreateAccount`) and, since the second `data_id` is attacker-known, sends the second `DataReceipt` directly to the recreated account. This satisfies `pending_data_count == 0` and triggers execution of the stale postponed receipt via `apply_action_receipt`, running against the new account's state/keys/balance as though it were a legitimately queued callback of the current account incarnation.

No existing check invalidates postponed receipts on `DeleteAccount`, and no code path ties the postponed receipt's continued validity to the account's "epoch"/incarnation — the fact that the account was deleted and recreated in between is invisible to `apply_data_receipt`/`apply_action_receipt`.

### Impact Explanation
This is authorization/privilege-boundary confusion: privileged pending computation associated with a prior account identity survives account teardown and executes against a distinct account state (potentially attacker-controlled contract/keys post re-creation), violating the value/authorization conservation invariant across account-identity boundaries. Concretely this can lead to unintended state mutation, receipt execution against unexpected account state, and permanently orphaned trie entries (state bloat) if the second dependency is never sent. This matches the "authorization escalation across accounts/promises" bounty category cited in the question.

### Likelihood Explanation
Preconditions are fully within an unprivileged attacker's control: deploy a contract producing a 2+ dependency promise combinator targeting itself, submit one satisfying `DataReceipt`, submit `DeleteAccount`, recreate the account, then submit the second `DataReceipt` (data_id is known to the attacker since they control the promise creation). All steps are ordinary transactions/receipts an unprivileged account can send; no validator/node privilege is required. The attack is repeatable and cheap (bounded by storage staking/gas fees).

### Recommendation
On `DeleteAccount`, iterate and remove all `TrieKey::PendingDataCount`, `TrieKey::PostponedReceiptId`, and `TrieKey::PostponedReceipt` entries associated with the deleted `receiver_id` (similar to how access keys/contract data are enumerated and purged in `remove_account`), or refuse `DeleteAccount` while any postponed receipts/pending data counts exist for the account, analogous to existing balance/lock checks in account deletion.

### Proof of Concept
Runtime apply-path integration test (test-loop or `runtime/runtime/src/tests/apply.rs` style):
1. Create account `attacker.near` with a contract exposing a callback requiring 2 input data ids (`promise.and`).
2. Submit the action receipt that schedules the postponed receipt with `input_data_ids.len() == 2`; assert `TrieKey::PendingDataCount{receiver: attacker.near, receipt_id}` == 2 and `TrieKey::PostponedReceipt` exists.
3. Submit first `DataReceipt` for `data_id_1`; assert `PendingDataCount` == 1, one `PostponedReceiptId` removed, `PostponedReceipt` still present.
4. Submit `DeleteAccount` action for `attacker.near`; assert `TrieKey::Account` is gone but `TrieKey::PendingDataCount`/`TrieKey::PostponedReceipt`/remaining `TrieKey::PostponedReceiptId` for `attacker.near` still return `Some(..)` from `get_pure`/`get_postponed_receipt`.
5. Recreate `attacker.near` via `CreateAccount`.
6. Submit second `DataReceipt` for the known `data_id_2` to `attacker.near`.
7. Assert `apply_action_receipt` executes the stale postponed receipt (e.g., via a state mutation or log emitted by the callback) against the newly created account, proving cross-incarnation state survival — this should not happen if account identity/lifecycle were correctly isolated.

### Citations

**File:** core/store/src/utils/mod.rs (L505-513)
```rust
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
    let mut gas_key_nonce_total_key_bytes: usize = 0;
```

**File:** core/store/src/utils/mod.rs (L551-573)
```rust
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
```

**File:** runtime/runtime/src/lib.rs (L1396-1472)
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
                    } else {
                        // There is still some pending data for the receipt, so we update the
                        // pending data count in the state.
                        set(
                            state_update,
                            TrieKey::PendingDataCount {
                                receiver_id: account_id.clone(),
                                receipt_id,
                            },
                            &(pending_data_count.checked_sub(1).ok_or_else(|| {
                                StorageError::StorageInconsistentState(
                                    "pending data count is 0, but there is a new DataReceipt"
                                        .to_string(),
                                )
                            })?),
                        );
                    }
```
