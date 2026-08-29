### Title
`resolve_promise_yield_timeouts` resumes stale yields by trie-key presence only, not account-lifetime identity - (File: runtime/runtime/src/lib.rs)

### Finding Description
The timeout-resolution loop in `resolve_promise_yield_timeouts` decides whether to fire a `PromiseResume` purely by checking `state_update.contains_key(&promise_yield_key, ...)` where `promise_yield_key = TrieKey::PromiseYieldReceipt { receiver_id: queue_entry.account_id, data_id: queue_entry.data_id }` [1](#0-0) . This is a pure trie-key-presence test on `(receiver_id, data_id)`; it carries no notion of "this is the same account instance that created the yield."

Critically, `remove_account` — invoked by `action_delete_account` when an account self-destructs — only clears `TrieKey::Account`, `TrieKey::ContractCode`, `TrieKey::AccessKey`/gas-key-nonce entries, and `TrieKey::ContractData`: [2](#0-1) [3](#0-2) . It never touches `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`/`DataIdToYieldId`, or the global `PromiseYieldTimeout` queue entry. Consequently, if a `PromiseYieldReceipt{A, data_id}` entry is already stored (via `set_promise_yield_receipt`, which is unconditional and account-existence-agnostic — see the `VersionedReceiptEnum::PromiseYield` branch that just calls `set_promise_yield_receipt(state_update, receipt)` with no account check) [4](#0-3) , deleting account `A` and later recreating an account literally named `A` (with an entirely new key set, owned by anyone) leaves that stale `PromiseYieldReceipt` entry intact and still keyed under the string `A`.

At the enqueued timeout height, `resolve_promise_yield_timeouts` reads the `PromiseYieldTimeout` queue entry (which is global/shard state, also unaffected by account deletion — see `enqueue_promise_yield_timeout`) [5](#0-4) , and its guard at line 3051 finds the stale `PromiseYieldReceipt{A, data_id}` still present, so it happily constructs and forwards a `PromiseResume` receipt destined to (the new) `A` [6](#0-5) . This resume is subsequently delivered to the `VersionedReceiptEnum::PromiseResume` handler, which fetches the *old* yield receipt via `get_promise_yield_receipt(state_update, account_id, data_id)` — again a pure key lookup with no ownership/lifetime check — and executes its embedded action receipt (`apply_action_receipt`) against whatever account/contract currently occupies name `A` [7](#0-6) .

### Impact Explanation
This confirms the structural root cause: the guard at `lib.rs:3051` is a per-name existence check on the trie key, not a per-account-incarnation identity check. Since `PromiseYield` receipts are self-callbacks (`predecessor_id == receiver_id == A`), the resumed action receipt executes with `predecessor_id` equal to the account name `A` against the *new* owner's live contract and state. Any `assert_self()`-style authorization check in the new owner's contract (comparing `predecessor_account_id == current_account_id`) will pass, because the check is a string comparison on the account name, not an identity/lifetime comparison. This allows the original (deleting) owner to pre-arm a callback with an attacker-chosen method name/args that will later execute as a "self call" inside a completely different party's contract, matching the "Authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
Preconditions match the stated setup: an unprivileged account creates a `promise_yield_create` callback on itself, then self-deletes via `DeleteAccount` before the yield resolves, and a third party (or the same attacker) later runs `CreateAccount` to reclaim the same account name before the timeout height is reached. All actions used (`FunctionCall` invoking `promise_yield_create`, `DeleteAccount`, `CreateAccount`) are ordinary, unprivileged transaction actions available to any funded account; no validator/node/peer access is required. The attack is fully repeatable and only costs standard gas/storage-staking fees.

### Recommendation
Either (a) have `remove_account` (and the `DeleteAccount` action path) proactively purge any outstanding `PromiseYieldReceipt`/`PromiseYieldStatus`/yield-id mapping entries for the account being deleted, and drop or invalidate any in-flight `PromiseYieldTimeout` queue entries referencing it, or (b) bind the yield/timeout state to an account-lifetime identifier (e.g., an incarnation counter or account nonce/creation height stored in `Account`) and have `resolve_promise_yield_timeouts` and the `PromiseResume` handler validate that identifier still matches the live account before resuming, rather than relying on trie-key presence alone.

### Proof of Concept
Unit test in `runtime/runtime/src/tests` (or `core/store/src/utils/mod.rs` tests) using a `TrieUpdate`:
1. `set_promise_yield_receipt` for account `"a.near"` with a fixed `data_id`, simulating a pending yield.
2. Call `remove_account(&mut state_update, &"a.near".parse().unwrap())`.
3. Assert `has_promise_yield_receipt(&state_update, "a.near".parse().unwrap(), data_id).unwrap() == true` — proving the entry survives account deletion.
4. Simulate `CreateAccount` for `"a.near"` (i.e., `set(state_update, TrieKey::Account{...}, &new_account)` with fresh keys) and re-assert `has_promise_yield_receipt(...) == true`, confirming the stale entry is still present and indistinguishable from a legitimately pending yield for the new account — exactly the condition `resolve_promise_yield_timeouts`'s `contains_key` check at `lib.rs:3051` relies on.

### Citations

**File:** runtime/runtime/src/lib.rs (L1495-1499)
```rust
            VersionedReceiptEnum::PromiseYield(_) => {
                // Received a new PromiseYield receipt. We simply store it and await
                // the corresponding PromiseResume receipt.
                set_promise_yield_receipt(state_update, receipt);
            }
```

**File:** runtime/runtime/src/lib.rs (L1511-1562)
```rust
                // Received a new PromiseResume receipt delivering input data for a PromiseYield.
                // It is guaranteed that the PromiseYield has exactly one input data dependency
                // and that it arrives first, so we can simply find and execute it.
                if let Some(yield_receipt) =
                    get_promise_yield_receipt(state_update, account_id, data_receipt.data_id)?
                {
                    // Remove the receipt from the state
                    remove_promise_yield_receipt(state_update, account_id, data_receipt.data_id);

                    // Clear the PromiseYield status
                    remove_promise_yield_status(state_update, account_id, data_receipt.data_id);

                    // Clean up yield_id <-> data_id mappings if this was created by yield_create_with_id
                    if ProtocolFeature::YieldWithId.enabled(apply_state.current_protocol_version) {
                        if let Some(yield_id) = get_yield_id_for_data_id(
                            state_update,
                            account_id,
                            data_receipt.data_id,
                        )? {
                            remove_yield_id_mappings(
                                state_update,
                                account_id,
                                yield_id,
                                data_receipt.data_id,
                            );
                        }
                    }

                    // Save the data into the state keyed by the data_id
                    set_received_data(
                        state_update,
                        account_id.clone(),
                        data_receipt.data_id,
                        &ReceivedData { data: data_receipt.data.clone() },
                    );

                    // Execute the PromiseYield receipt. It will read the input data and clean it
                    // up from the state.
                    return self
                        .apply_action_receipt(
                            state_update,
                            apply_state,
                            pipeline_manager,
                            &yield_receipt,
                            receipt_sink,
                            instant_receipts,
                            validator_proposals,
                            stats,
                            epoch_info_provider,
                            receipt_to_tx,
                        )
                        .map(Some);
```

**File:** runtime/runtime/src/lib.rs (L3046-3097)
```rust
        // Check if the yielded promise still needs to be resolved
        let promise_yield_key = TrieKey::PromiseYieldReceipt {
            receiver_id: queue_entry.account_id.clone(),
            data_id: queue_entry.data_id,
        };
        if state_update.contains_key(&promise_yield_key, AccessOptions::DEFAULT)? {
            let new_receipt_id = create_receipt_id_from_receipt_id(
                &queue_entry.data_id,
                apply_state.block_height,
                new_receipt_index,
            );
            new_receipt_index += 1;

            // Create a PromiseResume receipt to resolve the timed-out yield.
            let resume_receipt = Receipt::V0(ReceiptV0 {
                predecessor_id: queue_entry.account_id.clone(),
                receiver_id: queue_entry.account_id.clone(),
                receipt_id: new_receipt_id,
                receipt: ReceiptEnum::PromiseResume(DataReceipt {
                    data_id: queue_entry.data_id,
                    data: None,
                }),
            });

            // Record a ReceiptToTx entry for the new resume receipt. The parent is the
            // yield receipt that is being timed out.
            if processing_state.apply_state.save_receipt_to_tx {
                let yield_receipt: Receipt = get_pure(state_update, &promise_yield_key)?
                    .expect("promise yield receipt should exist since contains_key was true");
                processing_state.receipt_to_tx.push((
                    new_receipt_id,
                    ReceiptToTxInfo::V1(ReceiptToTxInfoV1 {
                        origin: ReceiptOrigin::FromReceipt(ReceiptOriginReceipt {
                            parent_receipt_id: *yield_receipt.receipt_id(),
                            parent_predecessor_id: yield_receipt.predecessor_id().clone(),
                        }),
                        receiver_account_id: queue_entry.account_id.clone(),
                        shard_id: processing_state.apply_state.shard_id,
                    }),
                ));
            }

            // The receipt is destined for the local shard and will be placed in the outgoing
            // receipts buffer. It is possible that there is already an outgoing receipt resolving
            // this yield if `yield_resume` was invoked by some receipt which was processed in
            // the current chunk. The ordering will be maintained because the receipts are
            // destined for the same shard; the timeout will be processed second and discarded.
            receipt_sink.forward_or_buffer_receipt(
                resume_receipt,
                apply_state,
                &mut state_update,
            )?;
```

**File:** core/store/src/utils/mod.rs (L181-198)
```rust
// Enqueues given timeout to the PromiseYield timeout queue
pub fn enqueue_promise_yield_timeout(
    state_update: &mut TrieUpdate,
    promise_yield_indices: &mut PromiseYieldIndices,
    account_id: AccountId,
    data_id: CryptoHash,
    expires_at: BlockHeight,
) {
    set(
        state_update,
        TrieKey::PromiseYieldTimeout { index: promise_yield_indices.next_available_index },
        &PromiseYieldTimeout { account_id, data_id, expires_at },
    );
    promise_yield_indices.next_available_index = promise_yield_indices
        .next_available_index
        .checked_add(1)
        .expect("Next available index for PromiseYield timeout queue exceeded the integer limit");
}
```

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
