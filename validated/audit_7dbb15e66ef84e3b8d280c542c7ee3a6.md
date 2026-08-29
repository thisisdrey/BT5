### Title
`remove_account` fails to purge PromiseYield state, letting a deleted+recreated account's surviving yield receipt execute on new account - (File: core/store/src/utils/mod.rs)

### Finding Description
`remove_account` in `core/store/src/utils/mod.rs` explicitly removes only `TrieKey::Account`, `TrieKey::ContractCode`, access keys/gas-key nonces, and `TrieKey::ContractData` for the account being deleted: [1](#0-0) [2](#0-1) 

It never iterates or removes `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldTimeout`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`, or `TrieKey::DataIdToYieldId` entries keyed to that `account_id`. These keys/rows are written by `set_promise_yield_receipt`, `enqueue_promise_yield_timeout`, `set_promise_yield_status`, and `set_yield_id_mapping`, and are explicitly designed to be removed by dedicated helper functions `remove_promise_yield_receipt`, `remove_promise_yield_status`, and `remove_yield_id_mappings`: [3](#0-2) [4](#0-3) [5](#0-4) 

However `remove_account` — the single cleanup routine invoked when `DeleteAccountAction` deletes an account — calls none of these three cleanup helpers. This is the asymmetry: the write path always seeds all four/five yield-related trie rows keyed by `(receiver_id, data_id/yield_id)` where `receiver_id` is the account, but the account-deletion path has no corresponding purge logic for them, unlike how it does purge access keys and contract data.

Given a contract on account `X` calls `promise_yield_create` (host function), the runtime writes a `PromiseYieldReceipt{receiver_id: X, data_id}` plus a `PromiseYieldTimeout` and `YieldIdToDataId`/`DataIdToYieldId` mapping rows. If `X` is subsequently deleted via `DeleteAccountAction` (which invokes `remove_account`) and later recreated via `CreateAccountAction`, these orphaned rows remain in the trie unaffected because `remove_account` never touches them. When the original `data_id` is later resolved (via `promise_yield_resume`, or naturally via the timeout queue processing), the lookup functions `get_promise_yield_receipt`/`get_data_id_for_yield_id` key strictly on `(receiver_id, data_id)`/`yield_id` — they do not carry or check any account "epoch"/creation-nonce to distinguish the old, deleted `X` from the newly created `X`. There is no invariant enforced anywhere in the store-utils layer that ties a `PromiseYieldReceipt` to a specific incarnation of the account; it is only ever addressed by `AccountId`.

I was not able to fully trace the resume-side execution logic in `runtime/runtime/src/lib.rs` within the available tool budget to confirm the exact receipt-dispatch code path and whether any additional guard exists there (e.g., checking account existence/genesis nonce at resume time). This should be verified before treating the impact as fully proven end-to-end, but the root-cause asymmetry in `remove_account` — omitting cleanup of `PromiseYieldReceipt`/`PromiseYieldTimeout`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` — is directly confirmed in the code.

### Impact Explanation
If confirmed end-to-end, this is an authorization-exactness violation: a receipt/callback created under the authority and state context of the original account `X` would execute against an unrelated, newly created account occupying the same `AccountId`, effectively a cross-account privilege leak (the new account owner did not create this yield, yet the stale receipt could deliver a resumed promise into it, potentially triggering contract logic or state changes on the new account's behalf). This matches the "authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
Preconditions are fully within an unprivileged attacker's control: deploy a contract that calls `promise_yield_create`, submit a `DeleteAccountAction` on the same account (attacker controls their own account's access key), then submit a `CreateAccountAction` to recreate it (or have another party create it, since account names are first-come/first-serve once deleted) before the yield times out. No validator, special permission, or leaked key is required — only ordinary transactions signed with the account's own keys. The main uncertainty is whether the resume/timeout dispatch code in `runtime/runtime/src/lib.rs` (not fully traced here) performs some indirect invalidation (e.g., failing when `get_account(new_X)` differs from expectations, or the receipt execution simply failing safely without recreating incorrect side effects) that would neutralize the impact — this needs verification via the runtime resume logic and an integration test before being confirmed as an exploitable end-to-end bug.

### Recommendation
In `remove_account`, before/while removing the account, iterate and remove all `PromiseYieldReceipt`, `PromiseYieldStatus`, and `YieldIdToDataId`/`DataIdToYieldId` rows scoped to `account_id` (analogous to how access keys and contract data are already iterated and purged), using the existing `remove_promise_yield_receipt`, `remove_promise_yield_status`, and `remove_yield_id_mappings` helpers. Also ensure the pending `PromiseYieldTimeout` queue entries for the deleted account are treated as no-ops at timeout-processing time (e.g., check account non-existence or a receipt-presence check, which appears already necessary since removing the receipt row means a subsequent timeout for a missing receipt should be skipped).

### Proof of Concept
Integration/unit test plan (runtime-test-loop or `runtime/runtime/src/tests`):
1. Deploy a contract on account `X` that calls `promise_yield_create`, capturing `data_id`.
2. Assert `TrieKey::PromiseYieldReceipt{receiver_id: X, data_id}`, `TrieKey::PromiseYieldTimeout`, `TrieKey::YieldIdToDataId`, `TrieKey::DataIdToYieldId` rows exist via `get_raw_prefix` iteration.
3. Submit `DeleteAccountAction` on `X` (beneficiary account funded), triggering `remove_account`.
4. Re-run the same `get_raw_prefix` iteration over the yield-related trie key prefixes for `account_id = X`: expect them to be gone, but assert (demonstrating the bug) that `PromiseYieldReceipt`/`YieldIdToDataId`/`DataIdToYieldId` rows are still present.
5. Submit `CreateAccountAction` recreating `X` (possibly with different code/owner).
6. Submit a `promise_yield_resume` (or let the timeout queue process) with the original `data_id`, and assert whether it resolves against the new `X`'s execution context — confirming cross-account leakage — or whether some other safeguard causes it to fail safely (which would need to be located and cited before treating this as fully exploitable).

### Citations

**File:** core/store/src/utils/mod.rs (L214-220)
```rust
pub fn remove_promise_yield_receipt(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::PromiseYieldReceipt { receiver_id: receiver_id.clone(), data_id });
}
```

**File:** core/store/src/utils/mod.rs (L273-279)
```rust
pub fn remove_promise_yield_status(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id });
}
```

**File:** core/store/src/utils/mod.rs (L326-334)
```rust
pub fn remove_yield_id_mappings(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    yield_id: YieldId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id });
    state_update.remove(TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id });
}
```

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

**File:** core/store/src/utils/mod.rs (L551-573)
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
```
