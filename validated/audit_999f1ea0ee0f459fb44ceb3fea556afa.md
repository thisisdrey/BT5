### Title
`remove_account` fails to purge `PromiseYieldReceipt`/`PromiseYieldStatus`/yield-id mappings, letting a stale yield be resumed via timeout against a deleted or re-created account - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` (core/store/src/utils/mod.rs:505-575) deletes the `Account`, `ContractCode`, access keys, gas-key nonces, and contract data trie entries for an account, but never removes `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, or the `YieldIdToDataId`/`DataIdToYieldId` mappings for that account. If a contract creates a yield and then self-deletes before the yield resolves, these entries survive in the trie past the account's death. When the timeout queue later fires a `PromiseResume{data: None}` at the deleted `account_id` (via `resolve_promise_yield_timeouts`), `process_receipt`'s `PromiseResume` branch (runtime/runtime/src/lib.rs:1500-1545) finds the leftover status/receipt still present and proceeds to execute/clean them up against an account that no longer exists in its original form.

### Finding Description
The relevant helper functions in `core/store/src/utils/mod.rs` are:
- `set_promise_yield_receipt` / `remove_promise_yield_receipt` / `get_promise_yield_receipt` (lines 200-228)
- `set_promise_yield_status` / `remove_promise_yield_status` / `get_promise_yield_status` (lines 241-279)
- `set_yield_id_mapping` / `remove_yield_id_mappings` (lines 281-334)

`remove_account` (lines 505-575) only clears the `Account`, `ContractCode`, access keys/gas-key nonces, and contract data — it never calls `remove_promise_yield_receipt`, `remove_promise_yield_status`, or `remove_yield_id_mappings` for the account being deleted. [1](#0-0) [2](#0-1) 

Attack flow:
1. Attacker X funds an account, deploys a contract, and issues a transaction that calls `promise_yield_create` (creating an entry via `set_promise_yield_receipt`/`set_promise_yield_status`/`enqueue_promise_yield_timeout`) and then, within the same or a subsequent transaction, executes `DeleteAccount` on itself.
2. `remove_account` runs and wipes the account/keys/code/data, but the `PromiseYieldReceipt`, `PromiseYieldStatus`, and yield-id mapping entries for `data_id` remain in the trie, keyed by the now-nonexistent `account_id`.
3. The previously-enqueued `PromiseYieldTimeout{account_id, data_id, expires_at}` entry is untouched by `remove_account` (it lives in an index-based queue processed independently by `resolve_promise_yield_timeouts`, referenced around lib.rs:2986).
4. When `expires_at` is reached, `resolve_promise_yield_timeouts` synthesizes a `PromiseResume{data: None}` receipt targeting the deleted `account_id`.
5. `process_receipt`'s `PromiseResume` branch (runtime/runtime/src/lib.rs:1500-1545) checks `get_promise_yield_status` — since the status was never set to `ResumeInitiated` (no legitimate resume occurred) it is not short-circuited, and `get_promise_yield_receipt` still returns `Some(yield_receipt)` because `remove_account` never cleared it. [3](#0-2) 
6. The code then calls `remove_promise_yield_receipt`, `remove_promise_yield_status`, optionally `remove_yield_id_mappings`, and `set_received_data` for `account_id`, and proceeds to make the postponed action receipt (created at yield-create time, targeting the same `account_id`) eligible for execution. [4](#0-3) 

Because the account no longer exists (or, if an unrelated party has since claimed/recreated the same `account_id`), this postponed action receipt executes against an account state that has nothing to do with the original yield's intent — either burning gas silently against a nonexistent account or applying unintended actions against a re-created account belonging to a different, unrelated party.

No existing check in `remove_account`, `resolve_promise_yield_timeouts`, or the `PromiseResume` handling validates that the target account still exists (or is the same account) before processing the timeout-triggered resume. This is a genuine gap: none of the standard signature/nonce/access-key/gas checks apply here because the flow is entirely receipt-driven and internal to the runtime after the initial `DeleteAccount` transaction is accepted.

### Impact Explanation
This falls under "state corruption" / cross-account authorization escalation: a postponed action receipt originally created and scoped to account X's yield can be delivered and applied to a different account that later reuses the same `account_id`, executing actions the new owner never authorized. In the simpler case (account not reused), it results in silently wasted gas and an inconsistent trie (dangling `PromiseYieldReceipt`/`PromiseYieldStatus` entries that get cleaned up asynchronously/incorrectly), a liveness/determinism concern rather than direct fund theft, but the cross-account case can constitute unauthorized state mutation of another account.

### Likelihood Explanation
Low cost, fully attacker-controlled: X only needs to fund one account, deploy a trivial contract that calls `promise_yield_create` then self-deletes via `DeleteAccount` before the yield resolves, and wait for the timeout. No privileged access, validator role, or special timing beyond normal transaction submission is required. The scenario is deterministically repeatable since it only depends on ordinary contract/wasm logic and standard transaction sequencing (yield-create tx followed by a delete-account tx, both within reach of `expires_at`).

### Recommendation
In `remove_account` (core/store/src/utils/mod.rs), before or during account removal, iterate and purge all `PromiseYieldReceipt`, `PromiseYieldStatus`, and `YieldIdToDataId`/`DataIdToYieldId` trie entries scoped to `account_id` (similar to how access keys and contract data are already enumerated and removed via `locked_iter` + prefix parsers), and/or filter/cancel any still-pending `PromiseYieldTimeout` queue entries referencing the deleted account so `resolve_promise_yield_timeouts` never emits a `PromiseResume` for it. Alternatively/additionally, the `PromiseResume` branch in `process_receipt` (runtime/runtime/src/lib.rs) should verify the target account still exists before honoring a stale timeout-triggered resume.

### Proof of Concept
Unit test in `core/store/src/utils/mod.rs` (or a runtime-level test alongside `test-loop-tests/src/tests/yield_timeouts.rs`):
1. Set up a `TrieUpdate`, call `set_promise_yield_receipt`, `set_promise_yield_status`, and `set_yield_id_mapping` for a given `account_id`/`data_id`/`yield_id`.
2. Call `remove_account(&mut state_update, &account_id)`.
3. Assert `get_promise_yield_receipt(&state_update, &account_id, data_id)` is `None`.
4. Assert `get_promise_yield_status(&state_update, &account_id, data_id)` is `None`.
5. Assert `get_data_id_for_yield_id`/`get_yield_id_for_data_id` return `None`.

Currently (per code read) steps 3-5 would return `Some(..)` since `remove_account` never removes these keys, demonstrating the bug. A full runtime/test-loop integration test would additionally drive a `promise_yield_create` + `DeleteAccount` transaction sequence, advance blocks past `expires_at`, and assert that the resulting `PromiseResume` timeout receipt either errors cleanly or is dropped, rather than executing a postponed action receipt against the deleted/recreated account.

### Citations

**File:** core/store/src/utils/mod.rs (L505-510)
```rust
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** core/store/src/utils/mod.rs (L571-575)
```rust
    for key in data_keys {
        state_update.remove(TrieKey::ContractData { account_id: account_id.clone(), key });
    }
    Ok(RemoveAccountResult { gas_key_nonce_count, gas_key_nonce_total_key_bytes })
}
```

**File:** runtime/runtime/src/lib.rs (L1500-1521)
```rust
            VersionedReceiptEnum::PromiseResume(data_receipt) => {
                if data_receipt.data.is_none() {
                    // This is a timeout resume. Check the status to see if the receipt has been resumed.
                    let status =
                        get_promise_yield_status(state_update, account_id, data_receipt.data_id)?;
                    if status == Some(PromiseYieldStatus::ResumeInitiated) {
                        // A non-timeout resume receipt has been sent, cancel the timeout.
                        return Ok(None);
                    }
                }

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
```

**File:** runtime/runtime/src/lib.rs (L1522-1545)
```rust

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
```
