### Title
Stale `PromiseYieldReceipt`/`PromiseYieldTimeout` entries survive `DeleteAccount` and execute attacker-authored callbacks against a recreated account - ([File: runtime/runtime/src/lib.rs], [File: core/store/src/utils/mod.rs])

### Summary
`remove_account` deletes `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` rows for a deleted account, but never clears `TrieKey::PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`, nor the global `PromiseYieldTimeout` queue entry that references the account. If the account is deleted while a yield is still pending and later recreated (same account id, different owner/key) before the yield's timeout height, `resolve_promise_yield_timeouts` still finds the stale `PromiseYieldReceipt` keyed by that account id and delivers the original, pre-deletion `FunctionCall` actions to whatever contract now lives there.

### Finding Description
- `promise_yield_create` stores the pending receipt at `TrieKey::PromiseYieldReceipt { receiver_id, data_id }` and schedules a global, non-account-scoped `TrieKey::PromiseYieldTimeout { index }` entry (`core/store/src/utils/mod.rs:181-212`, `runtime/runtime/src/function_call.rs:151-169`).
- `remove_account` (invoked from `action_delete_account`) only removes `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData`; it has no code path touching `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`: [1](#0-0) 
- When the timeout fires, `resolve_promise_yield_timeouts` looks the receipt up purely by `(queue_entry.account_id, queue_entry.data_id)` via `state_update.contains_key(&promise_yield_key, ...)`, with no check that the account still exists or is the same logical owner: [2](#0-1) 
- The resulting `PromiseResume` receipt is delivered locally and processed in `process_receipt`, where the stored `yield_receipt` (built at creation time, containing the pre-deletion `FunctionCall` actions, method name, arguments and gas) is executed via `apply_action_receipt` against whatever account state now exists at that id: [3](#0-2) 
- `promise_yield_create` always targets "the current account" (self-directed callback), per the host-function documentation: [4](#0-3)  so `predecessor_id == receiver_id` for the resumed receipt is the normal, designed self-call pattern.
- `check_actor_permissions` does not gate `FunctionCall` actions at all — they fall into the always-permitted branch: [5](#0-4) . This means the runtime never re-validates that the resumed callback's target contract is the same one that originally scheduled it.

Root cause: `remove_account` is incomplete relative to the yield/resume feature — it was written before (or without accounting for) the possibility that a `PromiseYieldReceipt`/`PromiseYieldTimeout` could reference a deleted account, so those columns are orphaned rather than purged on deletion.

### Impact Explanation
This is a determinism/authorization-exactness violation: a `FunctionCall` chosen and funded by the pre-deletion contract owner executes later against a completely different contract deployment (potentially a different owner), self-addressed (`predecessor_id == receiver_id`), which contracts commonly treat as a trusted "assert_self" callback. If the new contract at that account name relies on the `predecessor_id == current_account_id()` pattern to gate privileged logic (a standard NEAR idiom for promise callbacks), the stale receipt can trigger that privileged path with method name/arguments chosen by the previous, unrelated owner. This falls under "authorization escalation across accounts or promises."

However, the practical blast radius is narrow: exploiting this against a genuinely unrelated victim requires that the exact same account id be re-created by an entity who did not know about the pending yield. For a sub-account like `z.alice.near`, only the parent `alice.near` can issue the `CreateAccount` for that name, and only the account owner that deletes it controls when/if that happens — so the realistic scenario is largely self-inflicted (e.g. an account/name being sold or handed off with unnoticed pending yields), not an attacker forcing arbitrary third-party contracts to misbehave on demand.

### Likelihood Explanation
Preconditions are entirely reachable by an ordinary account: deploy a contract that calls `promise_yield_create`, then submit a self-directed `DeleteAccount`, then have the same (or delegated) authority recreate the account before `yield_timeout_length_in_blocks` elapses. No validator/node privilege is needed, and the whole sequence is deterministic and repeatable in a test-loop or unit-test harness. The narrowing factor is that recreating the identical account name generally requires cooperation of the same naming authority (parent account or registrar), which limits how often an unrelated victim would be exposed.

### Recommendation
Extend `remove_account` (`core/store/src/utils/mod.rs`) to also purge, for the deleted `account_id`: all `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` rows keyed by that account, mirroring the existing access-key/contract-data iteration-and-remove pattern. Additionally, `resolve_promise_yield_timeouts` should verify the account still exists (or that its "creation epoch"/nonce matches) before dispatching the resume receipt, so a stale timeout for a deleted-and-recreated account is silently dropped rather than delivered.

### Proof of Concept
Integration/test-loop test plan (based on existing `prepare_env_with_yield` / `yield_timeouts.rs` harness):
1. Deploy a contract to `z.alice.near` (as a sub-account of `alice.near`) that implements a yield/resume pattern (e.g. `call_yield_create_return_promise`) with a callback that performs a privileged, self-gated action (e.g. transfers funds or mutates critical state only when `predecessor_id() == current_account_id()`).
2. Submit a `FunctionCall` invoking `promise_yield_create`, capturing the `data_id` via `get_yield_data_ids_in_latest_state`.
3. Before the timeout height, submit `DeleteAccount(beneficiary_id = alice.near)` for `z.alice.near`.
4. Assert via direct trie inspection that `TrieKey::PromiseYieldReceipt`/`PromiseYieldStatus` for `z.alice.near` still exist in state (`get_promise_yield_receipt`/`get_promise_yield_status` return `Some`), proving `remove_account` missed them.
5. From `alice.near`, submit `CreateAccount` + `AddKey` for `z.alice.near` with a new key, and deploy a different contract (a "victim" contract with the same self-gated method name but different intended behavior).
6. Advance blocks past `yield_timeout_length_in_blocks` and assert that a `PromiseResume(data: None)` receipt is generated for `z.alice.near` and that `apply_action_receipt` executes the original stored `FunctionCall` against the newly deployed contract, mutating its state — expected assertion: the new contract's privileged/self-gated logic executes despite being triggered by actions authored before the account was recreated, confirming cross-deployment delivery of an un-invalidated yield callback.

### Citations

**File:** core/store/src/utils/mod.rs (L504-574)
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

**File:** runtime/runtime/src/lib.rs (L3046-3057)
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
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3621-3630)
```rust
    /// Creates a promise that will execute a method on the current account with given arguments
    /// and gas. The created promise will have a special input data dependency.
    ///
    /// A resumption token is written by this function into the register denoted by `register_id`.
    /// To satisfy the data dependency, call `promise_yield_resume` with the resumption token
    /// and a payload. The provided method will then be executed with input
    /// `PromiseResult::Successful(payload)`.
    ///
    /// The resumption token is portable across transactions, but only the current account
    /// is allowed to resolve this data dependency.
```

**File:** runtime/runtime/src/actions.rs (L739-785)
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
        Action::CreateAccount(_)
        | Action::FunctionCall(_)
        | Action::Transfer(_)
        | Action::TransferToGasKey(_) => (),
        Action::Delegate(_) | Action::DelegateV2(_) => (),
        Action::DeterministicStateInit(_) => (),
    };
    Ok(())
}
```
