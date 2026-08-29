This confirms the finding is valid: `resolve_promise_yield_timeouts` checks liveness purely by `TrieKey::PromiseYieldReceipt { receiver_id, data_id }` existence, and `remove_account` never removes `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` keys, so the same stale-authorization issue applies at timeout time with zero attacker action after account recreation.

### Title
Timeout-driven PromiseResume fires against a recreated account's surviving PromiseYieldReceipt, granting the deleted owner's callback for free - ([File: runtime/runtime/src/lib.rs], [File: core/store/src/utils/mod.rs])

### Finding Description
`resolve_promise_yield_timeouts` (runtime/runtime/src/lib.rs) pops entries from the `PromiseYieldTimeout` queue and, for each entry whose `expires_at <= block_height`, checks liveness solely via `state_update.contains_key(&TrieKey::PromiseYieldReceipt { receiver_id: queue_entry.account_id, data_id: queue_entry.data_id })` [1](#0-0) . If that key still exists, it synthesizes a `PromiseResume` `Receipt` with `data: None` addressed to `queue_entry.account_id` and enqueues/executes it, which will process the surviving `PromiseYieldReceipt`'s callback (e.g. `DeployContract`) as an ordinary receipt on whatever account currently occupies that `account_id` [2](#0-1) .

The root cause is that `remove_account` in `core/store/src/utils/mod.rs` only clears `TrieKey::Account`, `TrieKey::ContractCode`, access keys/gas keys, and `TrieKey::ContractData` — it never touches `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, or the `YieldIdToDataId`/`DataIdToYieldId` mappings [3](#0-2) . `TrieKey::PromiseYieldTimeout` queue entries are similarly untouched by account deletion. Since these keys are addressed only by `(receiver_id, data_id)` string/hash, not by any account-generation counter, an attacker can: (1) call `promise_yield_create` with a callback action (e.g. `DeployContract`) on their own account, which writes a `PromiseYieldReceipt` and a `PromiseYieldTimeout` queue entry; (2) `DeleteAccount` themselves; (3) recreate the same `account_id` via `CreateAccount`; (4) simply wait — no further attacker transaction is needed. Once chunk height passes `expires_at`, `resolve_promise_yield_timeouts` finds the stale `PromiseYieldReceipt` still present, builds a `PromiseResume` receipt, and the action receipt executor runs the yield's original callback actions against the *new* account, exactly as with a manually crafted resume, but triggered automatically by the protocol itself.

No existing signature/nonce/access-key checks apply here because the resuming receipt is synthesized internally by the runtime, not submitted by the attacker as a signed transaction — the runtime implicitly trusts that a live `PromiseYieldReceipt` key corresponds to the same logical account incarnation that created it, which is false after delete+recreate.

### Impact Explanation
This is an authorization-escalation bug: privileged callback actions (`DeployContract`, `FunctionCall`, `AddKey`, etc.) queued under the old account incarnation execute against the new account incarnation without any authorization from the new account's owner (who could even be a different party, since account names are first-come-first-served after deletion). This matches the "authorization escalation across accounts or promises" bounty category. The attacker's own subsequent action is unnecessary — the escalation is purely time-triggered — which arguably raises severity relative to the resume-crafting variant, since it fires unconditionally as long as the account is recreated with the same name before the timeout height.

### Likelihood Explanation
Preconditions are simple and fully within an unprivileged attacker's control: create a yield with a chosen callback, delete the account, recreate it with the same name, and wait for the chunk height to pass `expires_at` (yield timeout is a bounded, governance-configured number of blocks). No special gas, deposit, or contract logic beyond standard actions (`FunctionCall` invoking `promise_yield_create`, `DeleteAccount`, `CreateAccount`) is required, making this fully repeatable and low-cost.

### Recommendation
Bind `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`/`DataIdToYieldId` entries, and pending `PromiseYieldTimeout` queue entries to an account-incarnation identifier (e.g. a monotonically increasing account nonce/generation stored in the `Account` struct or a storage epoch), and have `remove_account` either purge these keys directly (if enumerable) or have `resolve_promise_yield_timeouts` and the resume-receipt path validate that the current account's incarnation matches the incarnation recorded at yield-creation time before executing the callback; otherwise drop/no-op the resume.

### Proof of Concept
Runtime test-loop integration test:
1. Deploy a contract on account `A` exposing a method that calls `promise_yield_create` with a callback action list containing `DeployContract` (attacker's malicious wasm) and a short/default timeout.
2. Submit `DeleteAccount` for `A`.
3. Submit `CreateAccount` (+ `Transfer` + `AddKey`) recreating `A` with the same account id, deploying a benign/empty contract.
4. Advance the test-loop chain height past `queue_entry.expires_at` (past the original yield's timeout height) without the new account owner submitting any resume-related transaction.
5. Assert that `resolve_promise_yield_timeouts` processed a timeout, that a `PromiseResume` receipt was generated and applied against account `A`, and that the callback's `DeployContract` action executed successfully — i.e., `A`'s code hash now matches the attacker-supplied wasm from step 1, not the benign contract deployed in step 3.

### Citations

**File:** runtime/runtime/src/lib.rs (L3046-3051)
```rust
        // Check if the yielded promise still needs to be resolved
        let promise_yield_key = TrieKey::PromiseYieldReceipt {
            receiver_id: queue_entry.account_id.clone(),
            data_id: queue_entry.data_id,
        };
        if state_update.contains_key(&promise_yield_key, AccessOptions::DEFAULT)? {
```

**File:** runtime/runtime/src/lib.rs (L3059-3068)
```rust
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
