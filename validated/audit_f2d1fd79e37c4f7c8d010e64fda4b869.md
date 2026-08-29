### Title
Stale `ReceivedData`/`PostponedReceipt` entries survive account deletion, letting a recreated account inherit attacker-forged promise-resolution state - (File: `core/store/src/utils/mod.rs`)

### Summary
`remove_account` only deletes `Account`, `ContractCode`, `AccessKey`/gas-key, and `ContractData` trie entries for a deleted account; it never removes `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, or `PromiseYieldReceipt` entries keyed to that `account_id`. Because these keys are addressed purely by the `AccountId` string (not by any per-account epoch/nonce), any leftover `ReceivedData` or postponed action receipt written by the original account survives deletion and can later be matched by receipts processed after a new, unrelated account is created under the same `AccountId`.

### Finding Description
`remove_account` in `core/store/src/utils/mod.rs` explicitly enumerates what it cleans up: [1](#0-0) 

It removes `Account`, `ContractCode`, access/gas keys, and `ContractData` — nothing else. `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` and sets `*account = None`, with no check for outstanding `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, or `PostponedReceipt` entries under that account: [2](#0-1) 

Meanwhile, the trie keys for these structures are namespaced solely by `AccountId`, with no per-incarnation nonce: [3](#0-2) 

`process_action_receipt` decides whether a receipt executes immediately or is postponed purely by checking `has_received_data(state_update, account_id, data_id)` against whatever is currently in the trie for that `account_id`: [4](#0-3) 

And `apply_action_receipt` collects `promise_results` from `ReceivedData` **before** it even loads/checks the receiver account (`account_did_not_exist` is only computed afterward), so a stale `ReceivedData` entry is consumed and turned into `PromiseResult::Successful(attacker_bytes)` regardless of which account currently owns that name: [5](#0-4) 

Exploit flow: account A (attacker) creates a self-targeted promise pair — an action receipt R1 (receiver=A) whose `output_data_receivers` route its result back to A as `ReceivedData{A, X}`, and a dependent callback receipt R2 (receiver=A, `input_data_ids=[X]`). If R1 is delayed (e.g. congested shard, so it sits in the delayed-receipt queue) while R2 has already been processed and stored as `PostponedReceipt`/`PendingDataCount` for A, A can submit `DeleteAccountAction` in a separate transaction that executes before R1. `remove_account` deletes the `Account` record but leaves `PostponedReceiptId{A,X}`, `PendingDataCount{A,R2}`, `PostponedReceipt{A,R2}` in place. When R1 eventually executes, it writes `ReceivedData{A,X,data=attacker_chosen}` (root cause bug also applies here, but note R1 might itself now fail since the account doesn't exist — the more direct path is R1 executing before deletion, leaving `ReceivedData{A,X}` behind after A deletes itself while R2 is still postponed elsewhere). Once a third party B later creates a new account under the identical `AccountId` (`CreateAccountAction`) and deploys its own contract, the still-pending R2 (whose `actions`/`method_name`/`args` were fixed by A before deletion) will eventually execute: `has_received_data`/`get_received_data` find the leftover entry, and R2's `FunctionCall` executes against B's live contract with a `PromiseResult` whose bytes were fully authored by the deleted A, not by any real cross-contract call the current (B-owned) contract initiated.

This breaks the invariant that `PromiseResult` content delivered to a `FunctionCall` is produced only by a receipt chain the *current* contract itself created, which is the trust assumption underlying `near-sdk`-style callback/"resolver" methods.

### Impact Explanation
This is a state-corruption / authorization-boundary bug: it lets an attacker plant forged "successful" promise-resolution data for an `AccountId` before giving it up, which a subsequent, unrelated occupant of that same name inherits. Concrete secondary impact (e.g. draining tokens) requires the new occupant's contract to expose a method whose name/argument shape happens to match what the attacker pre-registered in R2 and to trust `promise_result()` without an additional authorization check (e.g. `predecessor_id()`/`#[private]`) — this is a real but narrow precondition, not something the attacker can force. Absent that additional coincidence, the directly provable impact is unauthorized injection of attacker-controlled bytes into a `FunctionCall`'s promise inputs against an account the attacker no longer owns, which corresponds to the "authorization escalation across accounts or promises" bounty category rather than a guaranteed, self-contained fund-theft primitive.

### Likelihood Explanation
The precondition of a same-shard delay/reordering between R1, R2, and the `DeleteAccount` receipt is attacker-inducible (congestion can be self-triggered), and account-name recycling after deletion is permitted by the protocol (no "AccountId retirement"). Recreating the exact deleted `AccountId` requires either (a) the attacker themselves recreating it (no distinct victim, so limited value), or (b) waiting for/luring an unrelated third party to register that exact freed name and deploy a contract with a matching callback surface — a low-probability, largely uncontrollable event on a live network. This significantly limits real-world exploitability compared to the framing in the question.

### Recommendation
`remove_account` (and `action_delete_account`) should also purge any `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, and `PromiseYieldReceipt`/`PromiseYieldStatus`/yield-id-mapping entries associated with the deleted `account_id` (via prefix iteration, similar to how `ContractData` and access keys are already swept), or alternatively block `DeleteAccountAction` while such pending entries exist for the account.

### Proof of Concept
Integration test plan (`runtime/runtime/src/tests/apply.rs` style, similar to the existing `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code`):
1. Create account A, have it emit a self-targeted `output_data_receivers`/`input_data_ids` promise pair where R1 (data producer) is forced into the delayed queue (low gas limit / congestion) while R2 (consumer, postponed) is stored via `set_postponed_receipt`.
2. Apply a `DeleteAccountAction` receipt for A in a subsequent chunk; assert via direct trie reads that `TrieKey::PostponedReceipt{A,..}`/`PendingDataCount{A,..}` still exist post-deletion.
3. Let R1 execute (writing `ReceivedData{A,X}` with attacker-chosen bytes) and then have a `CreateAccountAction` + `DeployContractAction` recreate A with a benign contract exposing a method matching R2's `method_name`.
4. Let R2 finally execute; assert the deployed contract's method observed `promise_result(0) == PromiseResult::Successful(attacker_bytes)` even though the fresh contract never initiated any cross-contract call — demonstrating forged promise input delivery to an account that never authorized it.

### Citations

**File:** core/store/src/utils/mod.rs (L504-512)
```rust
/// Removes account, code and all access keys and gas keys associated to it.
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
```

**File:** runtime/runtime/src/actions.rs (L364-390)
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
}
```

**File:** core/primitives/src/trie_key.rs (L475-498)
```rust
            TrieKey::ReceivedData { receiver_id, data_id } => {
                buf.push(col::RECEIVED_DATA);
                buf.extend(receiver_id.as_bytes());
                buf.push(ACCOUNT_DATA_SEPARATOR);
                buf.extend(data_id.as_ref());
            }
            TrieKey::PostponedReceiptId { receiver_id, data_id } => {
                buf.push(col::POSTPONED_RECEIPT_ID);
                buf.extend(receiver_id.as_bytes());
                buf.push(ACCOUNT_DATA_SEPARATOR);
                buf.extend(data_id.as_ref());
            }
            TrieKey::PendingDataCount { receiver_id, receipt_id } => {
                buf.push(col::PENDING_DATA_COUNT);
                buf.extend(receiver_id.as_bytes());
                buf.push(ACCOUNT_DATA_SEPARATOR);
                buf.extend(receipt_id.as_ref());
            }
            TrieKey::PostponedReceipt { receiver_id, receipt_id } => {
                buf.push(col::POSTPONED_RECEIPT);
                buf.extend(receiver_id.as_bytes());
                buf.push(ACCOUNT_DATA_SEPARATOR);
                buf.extend(receipt_id.as_ref());
            }
```

**File:** runtime/runtime/src/lib.rs (L807-854)
```rust
        // Collecting input data and removing it from the state.
        let promise_results = if input_size_exceeded {
            for data_id in action_receipt.input_data_ids() {
                state_update.remove(TrieKey::ReceivedData {
                    receiver_id: account_id.clone(),
                    data_id: *data_id,
                });
            }
            Arc::from([])
        } else {
            action_receipt
                .input_data_ids()
                .iter()
                .map(|data_id| {
                    let ReceivedData { data } =
                        get_received_data(state_update, account_id, *data_id)?.ok_or_else(
                            || {
                                StorageError::StorageInconsistentState(
                                    "received data should be in the state".to_string(),
                                )
                            },
                        )?;
                    state_update.remove(TrieKey::ReceivedData {
                        receiver_id: account_id.clone(),
                        data_id: *data_id,
                    });
                    match data {
                        // TODO: Going from Vec<u8> to Rc<[u8]> shrinks the
                        // allocated buffer to fit, which may re-allocate if the
                        // capacity > len.
                        // Most likely, capacity == len holds here anyway but it
                        // would be better to use `Rc<u8>` already in `ReceivedData`
                        // and `DataReceipt`.
                        Some(value) => Ok(PromiseResult::Successful(Rc::from(value))),
                        None => Ok(PromiseResult::Failed),
                    }
                })
                .collect::<Result<Arc<[PromiseResult]>, RuntimeError>>()?
        };

        // state_update might already have some updates so we need to make sure we commit it before
        // executing the actual receipt
        state_update.commit(StateChangeCause::ActionReceiptProcessingStarted {
            receipt_hash: receipt.get_hash(),
        });

        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
```

**File:** runtime/runtime/src/lib.rs (L1608-1623)
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
```
