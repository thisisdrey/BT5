### Title
Stale `PendingDataCount`/`PostponedReceipt`/`PostponedReceiptId` trie entries survive account deletion, allowing a stuck postponed receipt to execute against a namesake account created later - (File: `core/store/src/utils/mod.rs`, `runtime/runtime/src/lib.rs`)

### Summary
`remove_account` deletes the `Account`, `ContractCode`, access-key, gas-key and `ContractData` trie entries for a deleted account, but never touches `TrieKey::PendingDataCount`, `TrieKey::PostponedReceipt`, or `TrieKey::PostponedReceiptId`. These are only cleared in the Data-receipt branch of `process_receipt` when the corresponding `DataReceipt` finally arrives. If an account is deleted while it still has a postponed `ActionReceipt` awaiting input data, those orphaned entries remain keyed to that `account_id` in the trie and will still be matched and executed if the same `account_id` string is later reused by a different account.

### Finding Description
`process_action_receipt` (`runtime/runtime/src/lib.rs:1593-1658`) stores a `PendingDataCount` and a `PostponedReceipt` (plus one `PostponedReceiptId` per missing input) whenever an `ActionReceipt` arrives with unresolved `input_data_ids` (the normal outcome of a contract using `promise_then`/batched promises). These are only consumed in the `Data`-receipt arm of `process_receipt` (`runtime/runtime/src/lib.rs:1386-1473`), which decrements `PendingDataCount` and, once it reaches zero, fetches and executes the stored `PostponedReceipt` via `apply_action_receipt` against whatever account currently sits at `receiver_id` in the trie.

`remove_account` (`core/store/src/utils/mod.rs:505-575`) is the only cleanup path invoked when an account is deleted (via the `DeleteAccount` action, `runtime/runtime/src/actions.rs`). It clears `Account`, `ContractCode`, access keys/gas-key nonces, and `ContractData`, but has no logic to enumerate or remove `PendingDataCount`, `PostponedReceipt`, or `PostponedReceiptId` entries for that `account_id`. [1](#0-0) [2](#0-1) 

Because trie keys for these entries are namespaced only by `account_id` (a string) and `receipt_id`, and NEAR account IDs are not permanently reserved once deleted, a different account creation event can later reuse the exact same `account_id` string. When the originally-dispatched `DataReceipt` (whose delivery timing an attacker who authored the promise chain can control) eventually arrives, `process_receipt`'s Data branch finds the leftover `PendingDataCount`/`PostponedReceipt` still indexed under that account name, decrements the counter to zero, and calls `apply_action_receipt` on the stale `PostponedReceipt`. That receipt's actions (which can include `AddKey`, `Transfer`, `FunctionCall`, `DeleteKey`, `Stake`, `DeleteAccount`, etc., since any promise-batch action list can carry input-data dependencies) then execute against the trie state of the *new* account occupying that name, not the account that originally authorized/received the receipt. This is a determinism/authorization break: the receipt's original execution context (built when the old account existed) is applied unmodified to a completely different account identity.

None of the existing signature, nonce, access-key, or storage-staking checks catch this because the vulnerability is entirely in server-side state bookkeeping — no signature or replay check is expected to fire on an internally generated receipt being delivered to its `receiver_id`; the check that is missing is account-identity/liveness verification before consuming a postponed receipt.

### Impact Explanation
This falls under "authorization escalation across accounts or promises." A postponed receipt carrying privileged actions (most notably `AddKey` with `FullAccess`, or arbitrary `FunctionCall`/`Transfer`/`DeleteAccount`) can execute against an account that has a different owner than the one for which the actions were originally queued, once that account name is deleted and later re-registered. In the worst case this grants an attacker-controlled key full access to a victim's freshly created account, or otherwise causes unauthorized state mutation on an account the attacker no longer (or never) legitimately controlled.

### Likelihood Explanation
- The attacker can trivially create the stuck-postponed-receipt precondition themselves (send a promise batch with `input_data_ids` to a target `receiver_id`, and simply withhold/delay the corresponding data receipt, which the attacker fully controls the timing of).
- The remaining precondition — the target `account_id` being deleted and later re-registered under a different owner — is not attacker-controlled and depends on normal account lifecycle events (users routinely delete and recreate/re-register account names, especially short/valuable ones). This makes the attack opportunistic rather than reliably attacker-triggerable end-to-end, reducing likelihood, but it does not require any social engineering, leaked keys, or privileged network access — it is a pure client/contract-level attack.
- Cost to the attacker is minimal (one deploy + a few cross-contract calls); repeatability is high since the attacker can seed many such stuck receipts against many account names speculatively.

### Recommendation
`remove_account` should enumerate and delete all `PendingDataCount`, `PostponedReceipt`, and `PostponedReceiptId` entries scoped to the deleted `account_id` (mirroring how it already iterates access keys and contract data), and any postponed receipt whose only remaining input data will never arrive should be resolved (e.g., converted into a failed execution outcome/refund) rather than silently persisted. Alternatively, bind postponed-receipt state to an account-instance identifier (e.g., a monotonically increasing account "epoch"/creation nonce) rather than solely to the reusable `account_id` string, so that a same-named but distinct account cannot ever satisfy a stale pending dependency.

### Proof of Concept
1. **Differential unit test** (as specified in the question): construct a `TrieUpdate`, write `TrieKey::PendingDataCount { receiver_id, receipt_id }` (plus a matching `TrieKey::PostponedReceipt`/`PostponedReceiptId`) via `set_postponed_receipt`/`set`, call `remove_account(&mut state_update, &receiver_id)`, then assert `get::<u32>(&state_update, &TrieKey::PendingDataCount{...})` and `get_postponed_receipt(...)` are `None`. This assertion fails today, proving the entries survive account deletion.
2. **Integration/runtime-test-loop PoC**:
   - Deploy a contract on account `A` that issues an `ActionReceipt` to `A` (or a subaccount) with `input_data_ids` pointing at a promise the caller controls, and whose action list includes `AddKey` (full access) with an attacker-supplied public key.
   - Do not deliver the dependent `DataReceipt` yet.
   - Have account `A` executed `DeleteAccount`.
   - Re-create account `A` from a different signer/owner (simulating account-name reuse), deposit funds, deploy a different contract.
   - Deliver the pending `DataReceipt` (attacker-controlled timing).
   - Assert that the attacker's public key now has a `FullAccess` `AccessKey` entry under the *new* `A` account (`get_access_key`), demonstrating unauthorized privilege escalation onto an account whose ownership had changed.

### Citations

**File:** core/store/src/utils/mod.rs (L505-512)
```rust
pub fn remove_account(
    state_update: &mut TrieUpdate,
    account_id: &AccountId,
) -> Result<RemoveAccountResult, StorageError> {
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });

    let mut gas_key_nonce_count: usize = 0;
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
