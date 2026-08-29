### Title
Stale self-authorized PromiseYieldReceipt survives `remove_account` and injects an unauthorized gas key on account recreation - (File: `core/store/src/utils/mod.rs`)

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) is the sole cleanup routine invoked by `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) when an account is deleted. It removes `TrieKey::Account`, `TrieKey::ContractCode`, all `AccessKey`/`GasKeyNonce` entries, and `ContractData`, but it never touches `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, or the global `TrieKey::PromiseYieldTimeout` queue entry that reference the deleted account as `receiver_id`. [1](#0-0) [2](#0-1) 

An account can create a self-targeted (`predecessor_id == receiver_id`) `promise_yield_create` receipt whose callback batch includes `promise_batch_action_add_gas_key_with_full_access`/`with_function_call`, giving itself an attacker-chosen gas key with arbitrary `num_nonces`/allowance/`receiver_id`/`method_names` once the promise resolves. That yielded receipt is persisted with `set_promise_yield_receipt` under `TrieKey::PromiseYieldReceipt{receiver_id: account_id, data_id}`, and a matching `TrieKey::PromiseYieldTimeout` entry is enqueued in the shard-global timeout queue. [3](#0-2) [4](#0-3) 

If the account is then deleted (`DeleteAccount`), the yield receipt/timeout entries are never purged. Later, when the account name is recreated (subaccount by its parent, or a named account whose FullAccess key/ownership has been transferred/sold to a new party), `resolve_promise_yield_timeouts` fires automatically at `expires_at` regardless of intervening deletion/recreation: it finds the still-present `PromiseYieldReceipt` for `receiver_id == account_id` and dispatches a `PromiseResume`, which `apply_action_receipt` executes via `get_promise_yield_receipt` → `apply_action_receipt(yield_receipt, ...)`. [5](#0-4) 

At execution time, `apply_action_receipt` seeds `actor_id = receipt.predecessor_id().clone()` (`runtime/runtime/src/lib.rs:855`), and each privileged action (`AddKey`) is authorized purely by string equality between `account_id` and `actor_id` via `check_actor_permissions` — it does not verify that the account is the "same" account instance that existed when the receipt was created; it only compares account-id strings, which are identical before and after deletion/recreation. Consequently the `AddKey` action for the gas key executes successfully against the freshly recreated (and otherwise empty) account, installing the attacker-chosen `AccessKeyPermission::GasKeyFullAccess`/`GasKeyFunctionCall` key with freshly-initialized `GasKeyNonce` entries (via `add_gas_key`), none of which were ever included in any transaction signed by the new account's current owner. [6](#0-5) [7](#0-6) 

### Impact Explanation
This is an authorization-escalation bug: a party who previously controlled an account/account-name can pre-plant a dormant, self-authorized `AddKey`(gas key) time-bomb, delete the account, and have it silently re-arm on the new owner's freshly (re)created account of the same name — without the new owner ever signing anything. Once armed, the attacker (holding the corresponding private key) can drain the account's balance/allowance via `FunctionCall`/`Delegate` transactions authorized by that gas key's `GasKeyNonce` sequence, which is freshly initialized to 0 and thus valid immediately. This falls under "authorization escalation across accounts or promises" and can lead to theft of user funds.

### Likelihood Explanation
Preconditions are fully within reach of an unprivileged, ordinary client: deploy a contract, call `promise_yield_create` targeting itself with an `AddKey` gas-key action in the callback, then self-issue `DeleteAccount`. No validator, RPC-operator, or protocol privilege is required — only standard actions (`FunctionCall`, `CreateAccount`, `AddKey`, `DeleteAccount`) plus the passage of `yield_timeout_length_in_blocks`, since the timeout fires automatically without any further attacker action. The scenario requires a subsequent, legitimate recreation of the same account name (e.g., parent-account resale/handoff, sub-account name reuse, or a sold named account), which is a supported and observed real-world NEAR practice (account/name trading), making it a realistic, repeatable attack rather than a purely theoretical one.

### Recommendation
`remove_account` must purge all pending `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` entries keyed by the deleted `account_id`, and either remove the corresponding `PromiseYieldTimeout` queue entries or make `resolve_promise_yield_timeouts`/the `PromiseResume` path re-verify that the account existed continuously since the yield was created (e.g., by tagging the yield receipt with an "account generation"/creation nonce, or discarding any leftover yield state whose target account no longer matches the state at creation) before executing privileged actions such as `AddKey`.

### Proof of Concept
Integration/test-loop test plan:
1. Create funded account `parent.near`; from it, create subaccount `sub.parent.near`.
2. From `sub.parent.near`, deploy a contract and call a method that does `promise_yield_create` targeting itself, with the resume callback batch containing `action_add_gas_key_with_full_access` (attacker-chosen `num_nonces`).
3. From `sub.parent.near`, submit `DeleteAccount` (beneficiary `parent.near`) before the yield resolves.
4. Advance blocks so `yield_timeout_length_in_blocks` elapses (triggering `resolve_promise_yield_timeouts` while the account doesn't exist — confirm no crash/leak persists in trie).
5. From `parent.near`, issue `CreateAccount` + fund `sub.parent.near` again (simulating a new "owner" receiving/reusing the name), without ever signing any `AddKey` for the gas key.
6. Assert via `ViewGasKeyNonces`/`view_access_key_query` that a `GasKeyFullAccess` `AccessKey` with `GasKeyNonce`-backed nonce slots now exists on the recreated `sub.parent.near`, matching the public key from step 2 — proving state leaked and executed across the account's deletion/recreation boundary without new-owner authorization.

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

**File:** core/store/src/utils/mod.rs (L551-575)
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
    Ok(RemoveAccountResult { gas_key_nonce_count, gas_key_nonce_total_key_bytes })
}
```

**File:** runtime/runtime/src/lib.rs (L691-701)
```rust
            Action::AddKey(add_key) => {
                metrics::ACTION_CALLED_COUNT.add_key.inc();
                action_add_key(
                    apply_state,
                    state_update,
                    account.as_mut().expect(EXPECT_ACCOUNT_EXISTS),
                    &mut result,
                    account_id,
                    add_key,
                )?;
            }
```

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

**File:** runtime/runtime/src/lib.rs (L3046-3068)
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
```

**File:** protocol-model/spec/runtime-execution.md (L79-79)
```markdown
`apply_action` seeds `ActionResult` with the action's `exec_fee` (gas/compute, `runtime/runtime/src/lib.rs:540`) and captures `current_contract` before running. It then runs `check_account_existence` (`runtime/runtime/src/actions.rs:824`) and `check_actor_permissions` (`runtime/runtime/src/actions.rs:776`); either failure returns early with the error set. Implicit account creation is allowed only when the action is the sole action and not a refund (`runtime/runtime/src/lib.rs:549`). Dispatch by action:
```
