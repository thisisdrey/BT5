### Title
Stale PromiseYieldReceipt survives account deletion and auto-fires attacker-controlled actions against a recreated account - (File: core/store/src/utils/mod.rs / runtime/runtime/src/lib.rs)

### Summary
`remove_account` in `core/store/src/utils/mod.rs` deletes `TrieKey::Account`, `ContractCode`, access/gas keys, and `ContractData`, but never removes `TrieKey::PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`/`DataIdToYieldId`, or the pending `PromiseYieldTimeout` queue entry associated with the deleted account. If a contract calls `promise_yield_create` and then self-deletes via `DeleteAccount`, the parked yield receipt (with an attacker-chosen `method_name`/`args` self-callback) survives in the trie under `receiver_id = A`. When the account name `A` is later recreated (by the attacker or by anyone else), the automatic timeout mechanism in `resolve_promise_yield_timeouts` (`runtime/runtime/src/lib.rs:3009-3105`) will still find the surviving `PromiseYieldReceipt` and synthesize a `PromiseResume`, which `process_receipt` (`lib.rs:1500-1562`) delivers via `apply_action_receipt` against whatever contract now occupies account `A`, executing the old attacker-defined `FunctionCall` action as a self-call.

### Finding Description
`set_promise_yield_receipt` (`core/store/src/utils/mod.rs:200-209`) stores a `PromiseYield` receipt keyed only by `(receiver_id, data_id)` — there is no linkage to any per-account "epoch"/creation nonce. The parked receipt is a self-receipt: `receiver_id == predecessor_id == account_id`, and it carries a `FunctionCall` action with attacker-chosen `method_name`/`args` (`Balance::ZERO` deposit, so no fund theft path), added by `promise_yield_create` (`runtime/near-vm-runner/src/logic/logic.rs:3660-3718`, `runtime/runtime/src/ext.rs:353-369`).

`action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` (`core/store/src/utils/mod.rs:504-575`), which only removes: [1](#0-0) 
access keys/gas keys, and contract data. It never enumerates or removes `TrieKey::PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId` rows for the account, nor the corresponding `PromiseYieldTimeout` queue entry that still references `account_id = A`.

The timeout queue entry is processed independent of account existence: [2](#0-1) 
`resolve_promise_yield_timeouts` only checks whether the `PromiseYieldReceipt` key still exists — it does not check whether the account was deleted/recreated in between. If it exists, a `PromiseResume` receipt targeting `A` is automatically synthesized and forwarded, with **no attacker interaction required post-recreation**.

When that `PromiseResume` is processed: [3](#0-2) 
the runtime fetches the stale receipt via `get_promise_yield_receipt(state_update, account_id, data_receipt.data_id)` and, if found, executes it via `apply_action_receipt` (`lib.rs:1549-1561`) against the account's *current* state — i.e., whatever contract now occupies `A` after `CreateAccount`. Because this is a self-receipt (`predecessor_id == receiver_id == A`), it passes as an authorized "self-call" even for methods gated with `#[private]`-style predecessor checks in the new contract, letting the attacker's stale, pre-chosen `method_name`/`args` execute against a completely different (possibly unrelated future) contract deployment, bypassing that contract's assumption that self-calls only originate from its own internal callback logic.

None of the existing checks stop this:
- `MAX_ACCOUNT_DELETION_STORAGE_USAGE` in `action_delete_account` only bounds the *account's own* tracked storage usage (code/keys/data), not the unrelated `PromiseYieldReceipt`/`PromiseYieldTimeout` trie rows, so deletion is not blocked by a pending yield.
- There is no check tying a `PromiseYieldReceipt`/timeout entry to a specific account "incarnation" (no account creation nonce is embedded in the trie key or in the queue entry).
- `CreateAccount` performs no cleanup or existence check against pending yield state for the account name being (re)created.

### Impact Explanation
This is an authorization-escalation / arbitrary-action-injection primitive: a stale parked receipt (created and shaped entirely by a previous account owner) fires automatically against whichever contract subsequently occupies the same account name, invoking the new contract's self-call surface with attacker-chosen `method_name`/`args`. Since NEAR account names are globally unique but reusable after deletion, this can affect any account name that gets recreated after a malicious prior tenant leaves behind a yield. Deposit is hardcoded to zero in `promise_yield_create`, so this is not a direct token-theft path, but it can trigger unintended state-mutating "internal" logic (e.g., privileged self-callbacks, resolution/finalization functions) on a victim's freshly deployed contract, matching the "authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
Preconditions are cheap and fully attacker-controlled: deploy a contract, call `promise_yield_create` (chooses `method_name`/`args`), then self-`DeleteAccount`, all in a single account's control, at negligible cost (yield-create fees + one delete-account transaction). No further attacker action is required to trigger the exploit — the protocol's own timeout mechanism (`yield_timeout_length_in_blocks`, e.g. ~200 blocks) automatically re-fires the parked receipt once the account name is reused. The residual risk depends on someone actually re-registering the exact account name within/after the timeout window, which is a real but not certain occurrence (attacker can also just recreate the account themselves to guarantee firing, though then there's no "victim"). This is fully repeatable per account name.

### Recommendation
When deleting an account in `action_delete_account`/`remove_account`, also enumerate and remove all `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, and `TrieKey::DataIdToYieldId` entries keyed by that `account_id` (similar to how access keys and contract data are enumerated via prefix iteration), and additionally invalidate/skip any still-queued `PromiseYieldTimeout` entries pointing at the deleted account (e.g., by checking account existence/creation identity, not just presence of the `PromiseYieldReceipt` key, in `resolve_promise_yield_timeouts` and in the `PromiseResume` handling path).

### Proof of Concept
Test-loop integration test extending `test_yield_then_resume_same_block` (`test-loop-tests/src/tests/yield_resume.rs`) / `test_simple_yield_timeout` (`test-loop-tests/src/tests/yield_timeouts.rs`):
1. Deploy test contract on account `A`; submit tx calling `call_yield_create_return_promise` with attacker-chosen args, producing a `PromiseYieldReceipt` for `A`.
2. In a following block, submit `DeleteAccount(A, beneficiary)`.
3. In a subsequent block, submit `CreateAccount(A)` (optionally deploying a different/benign contract with a `#[private]` self-callback method).
4. Advance blocks until `yield_timeout_length_in_blocks` elapses; assert that a `PromiseResume` targeting `A` is still produced (`find_yield_data_ids_from_latest_block`) and that `get_yield_data_ids_in_state`/`get_promise_yield_receipt` still returns the stale entry for the recreated account.
5. Assert (currently failing) that the parked receipt's `FunctionCall` action executes against the new contract at `A` — e.g., check for the callback's side effect/log — demonstrating that a name-reuse victim's contract had a self-call invoked without its own consent, and assert this should not occur (no yield receipt should fire after account recreation).

### Citations

**File:** core/store/src/utils/mod.rs (L509-510)
```rust
    state_update.remove(TrieKey::Account { account_id: account_id.clone() });
    state_update.remove(TrieKey::ContractCode { account_id: account_id.clone() });
```

**File:** runtime/runtime/src/lib.rs (L1511-1521)
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
```

**File:** runtime/runtime/src/lib.rs (L3046-3051)
```rust
        // Check if the yielded promise still needs to be resolved
        let promise_yield_key = TrieKey::PromiseYieldReceipt {
            receiver_id: queue_entry.account_id.clone(),
            data_id: queue_entry.data_id,
        };
        if state_update.contains_key(&promise_yield_key, AccessOptions::DEFAULT)? {
```
