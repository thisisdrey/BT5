### Title
Stale `PostponedReceiptId`/`PostponedReceipt`/`PendingDataCount` entries survive `DeleteAccount`, letting a pre-staged self-authorized receipt execute privileged actions against a later-recreated account - (`runtime/runtime/src/lib.rs`, `core/store/src/utils/mod.rs`)

### Summary
`remove_account` (called by `action_delete_account`) only removes `Account`, `ContractCode`, `AccessKey`/gas-key nonces, and `ContractData` trie entries; it never removes `PostponedReceiptId`, `PendingDataCount`, or `PostponedReceipt` entries keyed by the deleted account's ID. When the corresponding missing `DataReceipt` later arrives, `process_receipt` resolves the dependency using only `{receiver_id, data_id}` as a key and executes the stale `PostponedReceipt` via `apply_action_receipt` against whatever account currently exists under that name, with `actor_id` taken from the original (stale) receipt's `predecessor_id`.

### Finding Description
`remove_account` at [1](#0-0)  removes only `Account`, `ContractCode`, access/gas keys, and contract data — it never touches `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceipt` records, as confirmed by the full body of the function [2](#0-1) . `action_delete_account` calls `remove_account` and then simply clears the account and sets `actor_id` [3](#0-2) , performing no cleanup of dependency bookkeeping.

When an action receipt targeting account `A` has unmet `input_data_ids`, `process_action_receipt` writes `TrieKey::PostponedReceiptId{receiver_id: A, data_id}` → `receipt_id`, `TrieKey::PendingDataCount{A, receipt_id}`, and stores the full receipt under `TrieKey::PostponedReceipt{A, receipt_id}` [4](#0-3) . None of these keys are cleared by `DeleteAccount`.

When the awaited `Data` receipt eventually arrives, `process_receipt` looks up `PostponedReceiptId{receiver_id, data_id}` purely by name — with no check that the account is the same instance that existed when the postponed receipt was created — decrements `PendingDataCount`, and on reaching zero fetches and executes the stale `PostponedReceipt` via `apply_action_receipt` [5](#0-4) .

Inside `apply_action_receipt`, the account is re-fetched fresh from state (`get_account(state_update, account_id)`) — i.e., whatever account currently exists under that name, including one recreated after deletion — and `actor_id` is initialized from the stale receipt's own `predecessor_id`, not re-derived from any current signer or key [6](#0-5) . Crucially, `check_actor_permissions` authorizes the most privileged actions (`AddKey`, `DeployContract`, `DeleteKey`, `Stake`, `DeployGlobalContract`, `UseGlobalContract`, `WithdrawFromGasKey`, `DeleteAccount`) solely via **string equality** `actor_id == account_id` [7](#0-6)  — there is no account "incarnation"/epoch marker distinguishing the pre-deletion account from the post-recreation account. If the postponed receipt was originally a self-receipt (`predecessor_id == receiver_id == A`), `actor_id` will equal `A` again after recreation, so any privileged action embedded in the frozen receipt (e.g., `AddKey(FullAccess, attacker_pubkey)`, `DeployContract`) is authorized against the *new* incarnation of `A`, regardless of who currently owns/funds it.

### Impact Explanation
This is an authorization-exactness violation: the runtime treats account names as durable identities for authorization purposes even though account state (keys, balance, contract) is fully reset on deletion. If a different party — or the attacker under a different intended use — later re-creates the same account name (e.g., a freed-up subaccount, or a squatted top-level name once eligibility rules permit), a dangling self-authorized receipt from before the deletion can silently inject an attacker-controlled `FullAccess` key or malicious contract into the new account once its stale data dependency resolves, with no signature or fresh authorization from the new owner. This maps to NEAR's "authorization escalation across accounts or promises" bounty category, and can lead to theft of funds subsequently deposited into the recreated account, or to permanent compromise of a newly (re)created account.

### Likelihood Explanation
The attacker needs to: (1) own/control account `A` and deploy a contract to it, (2) engineer a promise chain that creates a self-receipt on `A` with an unresolved `input_data_id` (achievable by any account owner using standard cross-contract-call/callback patterns — no privileged access needed), (3) delete `A` via a normal `DeleteAccount` action (requires only owning `A`'s access key, which is under attacker's control), (4) get `A` re-created — trivially true if the attacker later re-creates their own subaccount `A`, and more speculative (but not per-se rejected) if a third party independently claims the freed name, and (5) wait for the naturally-produced `DataReceipt` from the promise chain to arrive (attacker can control the chain length/cross-shard hop count to create a window for steps 3–4). All steps are reachable purely through ordinary, unprivileged transactions/contract deployment; no validator, node-operator, or leaked-key access is required. The attack is fully repeatable at low cost (a few TGas/attonear for account creation/deletion cycles).

### Recommendation
On `DeleteAccount`, also enumerate and remove any dangling `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` entries for the account being deleted (mirroring the access-key/contract-data cleanup already done in `remove_account`), or alternatively bind postponed-receipt bookkeeping to an account "incarnation"/creation-nonce so that a data receipt arriving after account deletion+recreation cannot resolve a dependency created under a prior incarnation. At minimum, `apply_action_receipt` should refuse to execute a postponed receipt whose stored predecessor/receiver identity no longer matches a live, un-deleted-and-recreated account instance.

### Proof of Concept
Integration test outline (extending `runtime/runtime/src/tests/apply.rs` patterns, e.g. `test_promise_input_size_limit_exceeded_fails_and_cleans_up`):
1. Set up account `A` (e.g. as a subaccount of `attacker.near`) with a `FullAccess` key controlled by the attacker.
2. Submit an action receipt `R1` to `A` with `predecessor_id = A`, `input_data_ids = [data_id]`, and actions `[AddKey(FullAccess, attacker_backdoor_pubkey)]`. Apply this receipt alone (no matching `Data` receipt yet) and assert `get_postponed_receipt(state, A, R1.receipt_id)` is `Some` and `PostponedReceiptId{A, data_id}` is set.
3. Submit `DeleteAccountAction` for `A` (signed with the existing key) in a subsequent `apply` call; assert the account is gone (`get_account(state, A) == None`).
4. Submit `CreateAccountAction` for `A` from `attacker.near` (or a stand-in third party), giving it a *different* `FullAccess` key (simulating a new/unrelated owner), and confirm no access keys other than the new one exist.
5. Submit the missing `Data` receipt with the same `data_id`; assert (a) it is matched via the still-present `PostponedReceiptId{A, data_id}`, (b) `R1` executes via `apply_action_receipt`, and (c) after execution, `get_access_key(state, A, attacker_backdoor_pubkey)` returns `Some(FullAccess)` — i.e., the attacker's key was injected into the newly created, differently-owned account without any authorization from its current owner.
6. Assert this occurs despite no signature or transaction from the account's current legitimate owner authorizing the `AddKey` action against the *new* incarnation of `A`.

### Citations

**File:** core/store/src/utils/mod.rs (L505-509)
```rust
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
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

**File:** runtime/runtime/src/actions.rs (L750-776)
```rust
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

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1398-1455)
```rust
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
