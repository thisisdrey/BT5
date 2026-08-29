### Title
`remove_account` fails to clear `PromiseYieldReceipt`/`PromiseYieldStatus`/yield-id mappings for a deleted account, allowing a stale yield callback to later execute against a re-created account of the same name - (File: `core/store/src/utils/mod.rs`)

### Summary
`remove_account` in `core/store/src/utils/mod.rs` only removes `TrieKey::Account`, `TrieKey::ContractCode`, access keys/gas-key nonces, and `TrieKey::ContractData` for the deleted account, but never removes `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`/`TrieKey::DataIdToYieldId`, or the corresponding `TrieKey::PromiseYieldTimeout` queue entry written by `set_promise_yield_receipt`/`set_promise_yield_status`/`enqueue_promise_yield_timeout` in the same file. Because `TrieKey::PromiseYieldTimeout` entries are processed independently of whether the target account still exists, a receipt created before deletion can be delivered later to whichever account currently occupies that `AccountId` (including one created fresh after the deletion), because promise-yield callback delivery treats the callback's predecessor as the account itself and is not re-validated against the account owner at execution time.

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:505-575`) explicitly enumerates only four categories of state to purge: [1](#0-0) 
- `TrieKey::Account`
- `TrieKey::ContractCode`
- access keys / gas-key nonces (via prefix iteration)
- `TrieKey::ContractData` (via prefix iteration) [2](#0-1) 

It never touches:
- `TrieKey::PromiseYieldReceipt { receiver_id, data_id }`, written by `set_promise_yield_receipt` [3](#0-2) 
- `TrieKey::PromiseYieldStatus { receiver_id, data_id }`, written by `set_promise_yield_status` [4](#0-3) 
- `TrieKey::YieldIdToDataId`/`TrieKey::DataIdToYieldId`, written by `set_yield_id_mapping` [5](#0-4) 
- The `TrieKey::PromiseYieldTimeout` queue entry created by `enqueue_promise_yield_timeout` (indexed by a monotonically increasing global index, not by account, so it cannot be found or pruned from `remove_account` at all) [6](#0-5) 

Removal helpers for all of these keys exist (`remove_promise_yield_receipt`, `remove_promise_yield_status`, `remove_yield_id_mappings`), but none are invoked from `remove_account`, confirming the omission is not intentional API-shape limitation but a missed call site. [7](#0-6) [8](#0-7) [9](#0-8) 

Exploit flow: an attacker deploys a contract, calls a method that invokes `promise_yield_create` (writing `PromiseYieldReceipt`/`YieldIdToDataId` and enqueuing a `PromiseYieldTimeout` entry for its own `receiver_id`). Before the yield resolves or times out, the attacker submits `DeleteAccount` for that same account. `remove_account` deletes the account/code/keys/data but leaves the queued timeout entry and the postponed receipt in the trie, still keyed by the same `AccountId`. The attacker (or, more importantly, an unrelated third party who is unaware the name was previously used) can then submit `CreateAccount` for the identical `AccountId`. When the pending `PromiseYieldTimeout` is later dequeued (independent of whether an account currently exists at that id) or a matching `data_id` resolves, the stored `PromiseYieldReceipt` is delivered and executed against whatever account now occupies that name. Because promise-yield callback receipts are internally generated with `predecessor_id == receiver_id`, they are treated as self-calls and are authorized without checking the new account's actual access keys, so privileged actions embedded in the stale callback (e.g. adding a full-access key, deploying a contract, or transferring the new account's balance) execute against the new occupant's state and funds.

### Impact Explanation
This falls under "authorization escalation across accounts or promises" and potentially "theft ... of user funds": a receipt crafted under the old account's identity/authority is delivered to and executes with full self-actor privilege against a different account (the re-created one), which never authorized or even existed when the receipt was created. If a victim account is later created under the freed name, actions embedded in the attacker's stale callback (transfers, key additions, contract redeployment) can execute against the victim's funds/state without any signature or key check from the victim.

### Likelihood Explanation
Preconditions are cheap and fully within an ordinary user's capability: deploy a contract, call a method that invokes `promise_yield_create`, then submit `DeleteAccount` for the same account before the yield resolves/times out — all standard, unprivileged transactions. The only additional requirement for a fund-theft outcome is that a third party later creates an account with the identical, now-available `AccountId` and funds it before the queued timeout/resolution fires, which is speculative but not implausible for short-named or recycled account IDs; the state-leak/cross-account-delivery bug itself is fully attacker-triggerable and deterministic every time.

### Recommendation
Extend `remove_account` in `core/store/src/utils/mod.rs` to also purge all pending promise-yield state for the account being deleted: iterate/prune `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, and `TrieKey::YieldIdToDataId`/`TrieKey::DataIdToYieldId` entries for `account_id` (call `remove_promise_yield_status`/`remove_yield_id_mappings`/`remove_promise_yield_receipt`), and ensure the timeout-processing path in `runtime/runtime/src/lib.rs` checks account existence (or a generation/epoch marker) before delivering a queued `PromiseYieldTimeout` receipt, so a stale callback can never be routed to an account that did not exist when the yield was created.

### Proof of Concept
Runtime integration test (in `runtime/runtime/src/tests/apply.rs`, alongside existing yield tests):
1. Create account `A` with a contract that calls `promise_yield_create` with a callback action list containing a privileged action (e.g. `AddKey` with an attacker-controlled full access key) targeting `A` itself.
2. Apply the yield-create transaction; assert `TrieKey::PromiseYieldReceipt{receiver_id: A, data_id}` and a `TrieKey::PromiseYieldTimeout` entry exist in the trie.
3. Submit and apply `DeleteAccount` for `A`.
4. Assert `TrieKey::Account{A}` is gone, but `TrieKey::PromiseYieldReceipt{A, data_id}` and the `PromiseYieldTimeout` entry are still present (diff survivor trie keys against the full set from step 2) — this demonstrates the cleanup gap directly from `remove_account`.
5. Submit `CreateAccount` for `A` funded/owned by a different key.
6. Advance blocks until the timeout fires (or resolve via `promise_yield_resume`), and assert that the callback executes against the new `A`, e.g. that the attacker's embedded `AddKey` action succeeds and grants a full access key on the new account without the new owner's signature — confirming cross-account authorization escalation caused by the missing cleanup in `remove_account`.

### Citations

**File:** core/store/src/utils/mod.rs (L182-198)
```rust
pub fn enqueue_promise_yield_timeout(
    state_update: &mut TrieUpdate,
    promise_yield_indices: &mut PromiseYieldIndices,
    account_id: AccountId,
    data_id: CryptoHash,
    expires_at: BlockHeight,
) {
    set(
        state_update,
        TrieKey::PromiseYieldTimeout { index: promise_yield_indices.next_available_index },
        &PromiseYieldTimeout { account_id, data_id, expires_at },
    );
    promise_yield_indices.next_available_index = promise_yield_indices
        .next_available_index
        .checked_add(1)
        .expect("Next available index for PromiseYield timeout queue exceeded the integer limit");
}
```

**File:** core/store/src/utils/mod.rs (L200-212)
```rust
pub fn set_promise_yield_receipt(state_update: &mut TrieUpdate, receipt: &Receipt) {
    match receipt.versioned_receipt() {
        VersionedReceiptEnum::PromiseYield(action_receipt) => {
            assert!(action_receipt.input_data_ids().len() == 1);
            let key = TrieKey::PromiseYieldReceipt {
                receiver_id: receipt.receiver_id().clone(),
                data_id: action_receipt.input_data_ids()[0],
            };
            set(state_update, key, receipt);
        }
        _ => unreachable!("Expected PromiseYield receipt"),
    }
}
```

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

**File:** core/store/src/utils/mod.rs (L260-271)
```rust
pub fn set_promise_yield_status(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
    status: PromiseYieldStatus,
) {
    set(
        state_update,
        TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id },
        &status,
    );
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

**File:** core/store/src/utils/mod.rs (L281-297)
```rust
pub fn set_yield_id_mapping(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    yield_id: YieldId,
    data_id: CryptoHash,
) {
    set(
        state_update,
        TrieKey::YieldIdToDataId { receiver_id: receiver_id.clone(), yield_id },
        &data_id,
    );
    set(
        state_update,
        TrieKey::DataIdToYieldId { receiver_id: receiver_id.clone(), data_id },
        &yield_id,
    );
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
