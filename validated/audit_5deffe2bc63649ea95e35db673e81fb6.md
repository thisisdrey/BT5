### Title
Stale `YieldIdToDataId`/`PromiseYieldReceipt` state survives `DeleteAccount`, enabling resumption of a pre-deletion yield against a recreated account - ([File: core/store/src/utils/mod.rs])

### Summary
`remove_account` (called from `action_delete_account`) only clears `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` trie rows. It never removes `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId`, `TrieKey::PromiseYieldReceipt`, or `TrieKey::PromiseYieldStatus` entries. Consequently, a yield created with `promise_yield_create_with_id` before `DeleteAccount` remains fully resolvable and resumable after the account is deleted and recreated under the same name.

### Finding Description
`create_promise_yield_receipt_with_id` in `runtime/runtime/src/ext.rs:371-400` writes the bidirectional mapping via `set_yield_id_mapping` (`core/store/src/utils/mod.rs:281-297`) directly into the trie under `receiver_id = current_account_id`, and stores the pending callback content as `PromiseYieldReceipt{receiver_id, data_id}` once the receipt is delivered (`runtime/runtime/src/lib.rs:1495-1499`).

When `DeleteAccountAction` executes, `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` (`core/store/src/utils/mod.rs:504-575`), which only removes:
- `TrieKey::Account`
- `TrieKey::ContractCode`
- `TrieKey::AccessKey` / `TrieKey::GasKeyNonce`
- `TrieKey::ContractData`

It does **not** touch `YieldIdToDataId`, `DataIdToYieldId`, `PromiseYieldReceipt`, or `PromiseYieldStatus`, all of which are keyed by `receiver_id`/`account_id` but exist independently of the `Account` record's presence. [1](#0-0) [2](#0-1) 

After `CreateAccount(A)` recreates the account with fresh (zero) state, the stale `YieldIdToDataId{A, Y}` mapping is still present. `get_data_id_for_yield_id`/`has_yield_id_mapping` (`core/store/src/utils/mod.rs:299-324`) resolve it exactly as before, since these lookups do not check that the account currently exists or was created after the mapping. [3](#0-2) 

Calling `promise_yield_resume_with_yield_id(Y, payload)` in the new contract execution (as account A) invokes `submit_promise_resume_data_with_yield_id` (`runtime/runtime/src/ext.rs:428-440`), which finds the stale `data_id` and then `submit_promise_resume_data` (`runtime/runtime/src/ext.rs:402-426`) checks `has_promise_yield_receipt`/`has_promise_yield_status` — both still `true` because they were never removed — and issues a `PromiseResume` receipt targeting the pre-deletion `PromiseYieldReceipt`. That receipt was fully constructed (method name, arguments, gas) by the pre-deletion contract and will execute against whatever code is now deployed at account A, with the resumer's chosen `data` payload injected as the promise result.

This breaks the "authorization exactness" invariant: the resume mechanism assumes a yield can only be resolved against the same logical contract lifetime that created it, but the code allows a resume to reach across a `DeleteAccount` → `CreateAccount` boundary because cleanup of yield bookkeeping is incomplete.

### Impact Explanation
Category: authorization escalation via yield_id reuse across account recreation. If a new, unrelated party (or the same attacker with a different intended contract) reclaims account name `A`, a leftover callback set up by the deleted incarnation of `A` can still fire, invoking a method on the new contract with attacker-controlled resume `data`. Since such internal callbacks are typically gated only by `predecessor_id() == current_account_id()` (self-call), the stale resume passes that check trivially (receiver/predecessor are still `A`), letting an attacker trigger a "self-only" callback path with unexpected/attacker-chosen payload and outdated arguments that the new contract never intended to still be reachable.

### Likelihood Explanation
- Preconditions: `YieldWithId` protocol feature enabled (mainnet default), attacker deploys a contract to a funded account A calling `promise_yield_create_with_id` with a chosen `yield_id`, then deletes A via `DeleteAccountAction`, recreates A, and calls `promise_yield_resume_with_yield_id` with the same `yield_id`.
- Cost: minimal - one create-account, one function call, one delete-account, one create-account, one resume call. All ordinary, unprivileged transactions.
- Repeatability: fully deterministic and repeatable for any account the attacker controls; the risk to third parties depends on whether the account name is later reclaimed by someone else and whether `yield_id` values are guessable/leaked, but the core state-hygiene bug (stale trie rows surviving `DeleteAccount`) is unconditionally reproducible.

### Recommendation
In `remove_account` (or in `action_delete_account` before/after calling it), also purge all `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` trie rows scoped to the deleted `account_id`/`receiver_id`, mirroring how access keys and contract data are enumerated and removed via prefix iteration.

### Proof of Concept
Rust unit test in `runtime/runtime/src/actions.rs` (or a new test module colocated with `remove_account`):
1. Set up a `TrieUpdate` with an account `A`.
2. Call `create_promise_yield_receipt_with_id`-equivalent (or directly `set_yield_id_mapping` + `set_promise_yield_receipt`) to populate `YieldIdToDataId{A,Y}`, `DataIdToYieldId{A,data_id}`, `PromiseYieldReceipt{A,data_id}`, `PromiseYieldStatus{A,data_id}`.
3. Invoke `action_delete_account`/`remove_account` on `A`.
4. Assert `has_yield_id_mapping(&state_update, &A, Y)` still returns `true`, and `get_promise_yield_receipt`/`has_promise_yield_status` for `data_id` are still present — demonstrating the leftover state.
5. (Extended) Recreate account `A`, then call `submit_promise_resume_data_with_yield_id(Y, payload)` and assert it returns `Ok(true)`, proving the stale receipt is resumable post-recreation.

### Citations

**File:** core/store/src/utils/mod.rs (L299-324)
```rust
pub fn get_data_id_for_yield_id(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    yield_id: YieldId,
) -> Result<Option<CryptoHash>, StorageError> {
    get(trie, &TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id })
}

pub fn get_yield_id_for_data_id(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<YieldId>, StorageError> {
    get(trie, &TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id })
}

pub fn has_yield_id_mapping(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    yield_id: YieldId,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id },
        AccessOptions::DEFAULT,
    )
}
```

**File:** core/store/src/utils/mod.rs (L504-513)
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
```

**File:** runtime/runtime/src/actions.rs (L364-371)
```rust
    // We use current amount as a pay out to beneficiary.
    let account_balance = account_ref.amount();
    if account_balance > Balance::ZERO {
        result
            .new_receipts
            .push(Receipt::new_balance_refund(&delete_account.beneficiary_id, account_balance));
    }
    let remove_result = remove_account(state_update, account_id)?;
```
