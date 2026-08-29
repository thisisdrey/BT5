### Title
Postponed/received-data/PromiseYield state survives `DeleteAccount` and is later replayed against a re-created account via `CreateAccountAction` - ([File: runtime/runtime/src/actions.rs])

### Summary
`action_delete_account` (via `remove_account`) only clears the `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` trie rows for an account, and `action_create_account` initializes a brand-new `Account` row without checking or clearing any leftover `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, or `PromiseYieldReceipt`/`PromiseYieldStatus` rows keyed by that same `account_id`. Because these rows are keyed only by `(receiver_id, receipt_id/data_id)` with no account "generation" tag, a postponed cross-contract callback that was in flight when the old account was deleted can later be delivered and executed against the newly re-created account.

### Finding Description
`remove_account` in [1](#0-0)  removes only `Account`, `ContractCode`, access keys/gas-key nonces, and `ContractData` for `account_id`. It never touches `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId` rows, all of which are keyed by `receiver_id` per [2](#0-1) . `action_delete_account` calls this helper and then simply sets `*account = None` [3](#0-2) , with no additional cleanup of these queues.

`action_create_account` re-initializes the account purely based on naming rules (top-level/registrar or sub-account-of-predecessor checks) and unconditionally assigns a fresh, zero-balance `Account`: [4](#0-3) 
It performs no lookup of `has_received_data`, `get_postponed_receipt`, or any PromiseYield row for `account_id` before doing so — it assumes the namespace is clean because the `Account` row itself was gone.

When a `Data` receipt later arrives for that `account_id`, `process_receipt` looks up `TrieKey::PostponedReceiptId{receiver_id, data_id}` and, if the link is still present (because it was never cleared by delete/create), decrements `PendingDataCount` and, once it hits zero, fetches and executes the old `PostponedReceipt` via `apply_action_receipt` [5](#0-4) . `apply_action_receipt` itself just does `get_account(state_update, account_id)` to obtain whatever account currently resides at that id [6](#0-5)  — there is no check that this is the same account "generation" that originally created the postponed receipt.

Exploit flow (fully reachable by an unprivileged attacker controlling their own sub-account namespace, e.g. `x.attacker.near`):
1. Attacker's account `A = x.attacker.near` makes a cross-contract call whose callback (an `ActionReceipt` with an unmet `input_data_id`) targets `A` itself; this gets stored as `PostponedReceipt{receiver_id: A, receipt_id}` plus `PostponedReceiptId{receiver_id: A, data_id}` and `PendingDataCount{receiver_id: A, receipt_id}`.
2. Before the satisfying `Data` receipt is delivered, attacker submits `DeleteAccountAction` for `A` (beneficiary self). `remove_account` clears `Account`/keys/contract data but leaves the postponed-receipt rows intact.
3. Attacker submits `CreateAccountAction` for the same `A` (allowed — attacker is the parent `attacker.near`), followed by `Transfer`/`AddKey`, giving `A` a fresh zero balance and a new full-access key.
4. The originally pending `Data` receipt is delivered (its timing is determined by whatever produced it, e.g. attacker's own second contract, or a yield/resume the attacker controls). `process_receipt` finds the still-present `PostponedReceiptId` link, decrements `PendingDataCount` to zero, retrieves the stale `PostponedReceipt`, and executes its actions against the *new* `A`'s `Account` row — mutating the new owner's balance/storage_usage via actions the new owner never authorized.

No existing check (signature, nonce, access-key permission, or account-existence check) intervenes: the whole path operates on internal state trie keys that are invisible to and unauthenticated by the attacker's own subsequent transactions, and `apply_action_receipt` treats "account exists" as sufficient without verifying identity continuity.

### Impact Explanation
This breaks authorization exactness / state isolation across account identities: it lets a stale, unauthorized action receipt execute against a freshly created account and mutate its balance/storage state, i.e., state keyed by name silently carries over across deletion/recreation. This maps to authorization escalation across accounts/promises and potential loss/freezing of funds for the "new" owner's expectations, matching the NEAR bounty category for authorization escalation across accounts or promises.

### Likelihood Explanation
The attacker needs only an ordinary funded account able to create/delete its own sub-accounts (no validator/node/leaked-key access), deploy a contract, and control the timing of the receipt that resolves the postponed callback. This is fully repeatable and cheap — DeleteAccount/CreateAccount are ordinary actions, and orchestrating an unresolved cross-contract callback before deletion is standard contract logic.

### Recommendation
On `DeleteAccount`, also purge (or mark unusable) any `PostponedReceipt`/`PostponedReceiptId`/`PendingDataCount`/`ReceivedData`/`PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` rows keyed by that `account_id`, refunding/erroring out any postponed receipts rather than leaving them dangling; alternatively, tag these rows (or the `Account` record) with a per-account "generation" counter incremented on every `CreateAccountAction`, and have `apply_action_receipt`/`process_receipt` reject/refund postponed receipts whose recorded generation does not match the current account's generation.

### Proof of Concept
Integration/runtime test-loop plan:
1. Deploy a contract on account `A = "x.attacker.near"` that, in one receipt, issues a cross-contract call to another attacker-controlled contract `B` and registers a callback action receipt on `A` with an `input_data_id` (e.g. via `promise_then`), causing a `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` to be stored for `A`. The callback action, when eventually run, should perform an observable mutation (e.g. `Transfer` some amount into `A`, or a `FunctionCall` that writes/increments contract storage).
2. Before `B`'s response `Data` receipt is delivered, submit `DeleteAccountAction{beneficiary_id: attacker.near}` for `A`.
3. Submit `CreateAccountAction` + `Transfer` + `AddKey` for `A` again (new key, note the new starting balance/storage_usage).
4. Allow/force delivery of the deferred `Data` receipt satisfying the old callback's `input_data_id`.
5. Assert: the callback's actions execute (verified via `get_postponed_receipt`/state no longer present and via execution outcome), and `get_account(state_update, "x.attacker.near")` shows `amount()`/`storage_usage()` changed as a direct result of the stale callback — i.e., mutated relative to the fresh account created in step 3, despite the new account's owner never having submitted or authorized that specific callback action.

### Citations

**File:** core/store/src/utils/mod.rs (L504-575)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
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

**File:** core/primitives/src/trie_key.rs (L203-247)
```rust
    /// purposes to avoid deserializing the entire receipt.
    PostponedReceiptId {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::POSTPONED_RECEIPT_ID,
    /// Used to store the number of still missing input data `u32` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PendingDataCount {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::PENDING_DATA_COUNT,
    /// Used to store the postponed receipt `primitives::receipt::Receipt` for a given receiver's
    /// `AccountId` and a given `receipt_id` of the receipt.
    PostponedReceipt {
        receiver_id: AccountId,
        receipt_id: CryptoHash,
    } = col::POSTPONED_RECEIPT,
    /// Used to store indices of the delayed receipts queue (`node-runtime::DelayedReceiptIndices`).
    /// NOTE: It is a singleton per shard.
    DelayedReceiptIndices = col::DELAYED_RECEIPT_OR_INDICES,
    /// Used to store a delayed receipt `primitives::receipt::Receipt` for a given index `u64`
    /// in a delayed receipt queue. The queue is unique per shard.
    DelayedReceipt {
        index: u64,
    } = 8,
    /// Used to store a key-value record `Vec<u8>` within a contract deployed on a given `AccountId`
    /// and a given key.
    ContractData {
        account_id: AccountId,
        key: Vec<u8>,
    } = col::CONTRACT_DATA,
    /// Used to store head and tail indices of the PromiseYield timeout queue.
    /// NOTE: It is a singleton per shard.
    PromiseYieldIndices = col::PROMISE_YIELD_INDICES,
    /// Used to store the element at given index `u64` in the PromiseYield timeout queue.
    /// The queue is unique per shard.
    PromiseYieldTimeout {
        index: u64,
    } = col::PROMISE_YIELD_TIMEOUT,
    /// Used to store the postponed promise yield receipt `primitives::receipt::Receipt`
    /// for a given receiver's `AccountId` and a given `data_id`.
    PromiseYieldReceipt {
        receiver_id: AccountId,
        data_id: CryptoHash,
    } = col::PROMISE_YIELD_RECEIPT,
```

**File:** runtime/runtime/src/actions.rs (L167-210)
```rust
pub(crate) fn action_create_account(
    fee_config: &RuntimeFeesConfig,
    account_creation_config: &AccountCreationConfig,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    account_id: &AccountId,
    predecessor_id: &AccountId,
    result: &mut ActionResult,
) {
    if account_id.is_top_level() {
        if account_id.len() < account_creation_config.min_allowed_top_level_account_length as usize
            && predecessor_id != &account_creation_config.registrar_account_id
        {
            // A short top-level account ID can only be created registrar account.
            result.result = Err(ActionErrorKind::CreateAccountOnlyByRegistrar {
                account_id: account_id.clone(),
                registrar_account_id: account_creation_config.registrar_account_id.clone(),
                predecessor_id: predecessor_id.clone(),
            }
            .into());
            return;
        } else {
            // OK: Valid top-level Account ID
        }
    } else if !account_id.is_sub_account_of(predecessor_id) {
        // The sub-account can only be created by its root account. E.g. `alice.near` only by `near`
        result.result = Err(ActionErrorKind::CreateAccountNotAllowed {
            account_id: account_id.clone(),
            predecessor_id: predecessor_id.clone(),
        }
        .into());
        return;
    } else {
        // OK: Valid sub-account ID by proper predecessor.
    }

    *actor_id = account_id.clone();
    *account = Some(Account::new(
        Balance::ZERO,
        Balance::ZERO,
        AccountContract::None,
        fee_config.storage_usage_config.num_bytes_account,
    ));
}
```

**File:** runtime/runtime/src/actions.rs (L371-389)
```rust
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

**File:** runtime/runtime/src/lib.rs (L853-854)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
```

**File:** runtime/runtime/src/lib.rs (L1395-1454)
```rust
                // Check if there is already a receipt that was postponed and was awaiting for the
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
```
