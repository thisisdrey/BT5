### Title
Deleting an account while it has postponed/pending receipts leaves stale `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` state that later executes against a recreated account - (File: `core/store/src/utils/mod.rs`)

### Summary
`remove_account` (core/store/src/utils/mod.rs:505-575), invoked by `action_delete_account` (runtime/runtime/src/actions.rs:314-390), removes `Account`, `ContractCode`, `AccessKey`/gas-key entries and `ContractData`, but never removes `TrieKey::PostponedReceipt`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceiptId`. These three key types are exactly the ones written by `process_action_receipt` (runtime/runtime/src/lib.rs:1593-1658) whenever an incoming `ActionReceipt` targeting the account has unresolved `input_data_ids`. If the account is deleted and later recreated before the missing `DataReceipt` arrives, the stale postponed receipt is still executed once the data completes the pending count, running against whatever account now occupies that `account_id`.

### Finding Description
`process_action_receipt` stores, per `receiver_id`:
- `TrieKey::PostponedReceiptId` for every unresolved `data_id` [1](#0-0) 
- `TrieKey::PendingDataCount` and the serialized `TrieKey::PostponedReceipt` when `pending_data_count > 0` [2](#0-1) 

`remove_account` only clears `Account`, `ContractCode`, access/gas keys, and `ContractData`; it has no logic touching `PostponedReceipt`, `PendingDataCount`, or `PostponedReceiptId` for the account [3](#0-2) . `action_delete_account`, which calls `remove_account`, only checks storage-usage size and gas-key balance limits before deleting — it performs no check for outstanding postponed/pending receipts targeting the account [4](#0-3) .

When the delayed `DataReceipt` eventually arrives, the runtime's data-receipt handling decrements `PendingDataCount`, and upon reaching zero, looks up and executes the stored `PostponedReceipt` by fetching the (now current) account state fresh via `get_account` at the time of execution (as seen at the top of `apply_action_receipt`, `runtime/runtime/src/lib.rs:853-855`), i.e. it applies to whatever account currently exists at that `account_id`, not the account that existed when the receipt was postponed. There is no existence/identity binding recorded with the postponed receipt to invalidate it if the account was deleted and recreated in the interim.

Exploit flow:
1. Attacker sends a `FunctionCall` receipt to `victim.near` that issues a self-callback promise (`then` on itself), producing a second `ActionReceipt` with non-empty `input_data_ids` targeting `victim.near`. This writes `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` for `victim.near`.
2. Before the corresponding `DataReceipt` is delivered, `victim.near`'s owner (or the account owner itself, e.g. attacker's own account for self-inflicted testing, or via a compromised/careless key) submits `DeleteAccount`. `remove_account` clears the account but leaves the three postponed-receipt trie entries intact.
3. Attacker submits `CreateAccount` to recreate `victim.near` (implicit or named account creation, funded by attacker).
4. The delayed `DataReceipt` arrives, decrements `PendingDataCount` to 0, and the stale `PostponedReceipt` is executed against the newly created account, running its leftover `Action`s (e.g. a `FunctionCall`) as if authorized by the new account owner.

No existing check (nonce, access-key permission, `check_actor_permissions`, storage-staking, or size limits) inspects or blocks this sequence, since deletion and recreation are each individually valid actions, and the postponed-receipt bookkeeping is scoped only by `account_id`, which is reusable after deletion.

### Impact Explanation
This is authorization escalation across account lifetimes: an attacker (or an unaware, ordinary victim.near owner) can end up with a stale receipt from a prior "epoch" of the account executing against a brand-new account instance, in violation of state determinism/authorization exactness. Depending on the leftover receipt's actions (e.g. `FunctionCall` with privileged arguments, `Transfer`, `AddKey`), this can lead to unauthorized state changes or fund movement on the resurrected account, matching the "authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
The preconditions require deliberate account-lifecycle timing: a contract that creates a self-callback (common pattern for two-phase workflows), a `DeleteAccount` executed before the async data resolves, and prompt recreation of the same `account_id`. This is entirely reachable by an ordinary unprivileged user controlling their own account and contract, using only standard transactions (`FunctionCall`, `DeleteAccount`, `CreateAccount`) — no validator or node privilege needed. The attack is repeatable and costs only the gas/storage-staking deposit for account creation/deletion cycles.

### Recommendation
`remove_account` should also enumerate and delete any `TrieKey::PostponedReceipt`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceiptId` entries scoped to the deleted `account_id` (analogous to how it clears access keys and contract data via prefix iteration). Alternatively/additionally, `action_delete_account` should refuse to delete an account that still has any postponed/pending receipts registered against it (similar to the existing storage-usage and gas-key-balance guards), returning an `ActionError` until those receipts are resolved or explicitly refunded/dropped.

### Proof of Concept
Runtime/test-loop integration test:
1. Deploy a contract to `victim.near` that, on a `FunctionCall`, issues `promise_then` on itself creating a receipt with non-empty `input_data_ids` (e.g. a cross-contract call to another account followed by a callback to itself).
2. Submit the `FunctionCall` and assert (via direct trie query) that `TrieKey::PostponedReceipt { receiver_id: victim.near, .. }`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceiptId` exist for `victim.near`.
3. Submit `DeleteAccount(victim.near)` in a subsequent block/receipt and assert it succeeds.
4. Immediately query the trie for `TrieKey::PostponedReceipt { receiver_id: victim.near, .. }` and assert it still returns `Some(..)` (demonstrating `remove_account`'s clear-set does not cover `process_action_receipt`'s write-set).
5. Recreate `victim.near` via `CreateAccount`, then deliver the delayed `DataReceipt` and assert the stale postponed receipt executes against the new account (e.g. observable side effect like a state write or balance change attributable to the old receipt's actions), confirming cross-lifetime execution.

### Citations

**File:** runtime/runtime/src/lib.rs (L1609-1623)
```rust
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
```

**File:** runtime/runtime/src/lib.rs (L1642-1655)
```rust
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

**File:** runtime/runtime/src/actions.rs (L314-390)
```rust
pub(crate) fn action_delete_account(
    state_update: &mut TrieUpdate,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    receipt: &Receipt,
    result: &mut ActionResult,
    account_id: &AccountId,
    delete_account: &DeleteAccountAction,
    config: &RuntimeConfig,
    current_protocol_version: ProtocolVersion,
) -> Result<(), StorageError> {
    let account_ref = account.as_ref().unwrap();
    let account_storage_usage = if ProtocolFeature::FixDeleteAccountGlobalContractStorageUsage
        .enabled(current_protocol_version)
    {
        let contract_storage = get_contract_storage_usage(state_update, account_id, account_ref)?;
        account_ref.storage_usage().saturating_sub(contract_storage)
    } else {
        // Legacy behavior: only subtracts local contract code, misses the
        // global contract identifier overhead.
        let account_storage_usage = account_ref.storage_usage();
        let code_len = get_code_len_or_default(
            state_update,
            account_id.clone(),
            account_ref.local_contract_hash().unwrap_or_default(),
        )?;
        debug_assert!(
            code_len == 0 || account_storage_usage > code_len,
            "account storage usage should be larger than code size. storage usage: {}, code size: {}",
            account_storage_usage,
            code_len
        );
        account_storage_usage.saturating_sub(code_len)
    };
    if account_storage_usage > Account::MAX_ACCOUNT_DELETION_STORAGE_USAGE {
        result.result =
            Err(ActionErrorKind::DeleteAccountWithLargeState { account_id: account_id.clone() }
                .into());
        return Ok(());
    }
    let gas_key_balance_to_burn = compute_gas_key_balance_sum(state_update, account_id)?;
    if gas_key_balance_to_burn > GasKeyInfo::MAX_BALANCE_TO_BURN {
        result.result = Err(ActionErrorKind::GasKeyBalanceTooHigh {
            account_id: account_id.clone(),
            public_key: None,
            balance: gas_key_balance_to_burn,
        }
        .into());
        return Ok(());
    }
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
}
```
