### Title
Stale `TrieKey::PromiseYieldReceipt`/`PromiseYieldStatus` survive `DeleteAccount` and are replayable against a re-created account of the same name - (File: `core/store/src/utils/mod.rs`, `runtime/runtime/src/ext.rs`)

### Summary
`remove_account` in `core/store/src/utils/mod.rs` deletes the `Account`, `ContractCode`, access keys and contract data, but never removes `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::PromiseYieldTimeout`, or the `YieldIdToDataId`/`DataIdToYieldId` mappings created by an earlier `yield_create`. Because `TrieKey::PromiseYieldReceipt` and the lookup functions `has_promise_yield_receipt`/`get_promise_yield_receipt` are keyed only by `(receiver_id, data_id)` with no per-incarnation nonce, a stale receipt survives account deletion and becomes indistinguishable from a legitimate live yield belonging to a subsequently re-created account of the same name.

### Finding Description
`set_promise_yield_receipt` stores the full callback `Receipt` (its `ActionReceipt`, including its action list) under `TrieKey::PromiseYieldReceipt { receiver_id, data_id }` when `yield_create` runs [1](#0-0) . When `DeleteAccount` is processed, `remove_account` clears only the account, code, keys and contract data, and does not touch `PromiseYieldReceipt`, `PromiseYieldStatus`, `PromiseYieldTimeout`, or the yield-id mappings [2](#0-1) .

Resumption is gated purely on trie presence keyed by `(account_id, data_id)`: `submit_promise_resume_data` calls `has_promise_yield_receipt(self.trie_update, self.account_id.clone(), data_id)` / `has_promise_yield_status(...)`, and if either is true it schedules a `PromiseResume` receipt and marks the status `ResumeInitiated` [3](#0-2) . There is no account “incarnation” or epoch discriminator anywhere in this key or check — only the account name and the `data_id`.

Exploit flow:
1. Original owner of account `A` runs `yield_create`, producing a postponed `PromiseYieldReceipt` under key `(A, data_id)` whose stored `ActionReceipt` actions encode the original callback logic (e.g. a `FunctionCall`/`Transfer` executed against `A`).
2. `A` is deleted via `DeleteAccount` before the yield is resumed or times out; `remove_account` leaves the `PromiseYieldReceipt`/`PromiseYieldStatus` rows in the trie untouched.
3. A new, unrelated owner submits `CreateAccount` for the same `account_id` `A`, deploys their own contract, and deposits funds.
4. Anyone who knows the old `data_id` (the original owner, or anyone who observed it on-chain, since `data_id` is derived deterministically from action hash/receipt id and is publicly visible in past transactions) calls a method on the new `A` contract that invokes the `promise_yield_resume` host function with that old `data_id`. `has_promise_yield_receipt`/`has_promise_yield_status` return `true` against the stale row, so the resume is accepted and the runtime eventually delivers `ReceivedData` for that `data_id`, which satisfies the still-present postponed `PromiseYieldReceipt` and causes its stored (stale) `ActionReceipt` actions to execute against `A`’s current state — now funded by the new owner.

Because the check compares only `(receiver_id, data_id)` and never anything unique to the account's post-recreation "identity", none of the standard signature/access-key/nonce checks apply here: the resumed receipt is not a fresh signed transaction but a stored, pre-existing internal `Receipt` object replayed from state.

### Impact Explanation
This allows an attacker with knowledge of a pre-deletion `data_id` to trigger replay of a stale callback `ActionReceipt` against a newly created, unrelated account of the same name, executing actions (potentially fund transfers) that were baked into the receipt before deletion but now run against the new account's balance. This maps to theft of user funds and falls under NEAR's fund-theft / authorization-escalation bounty category.

### Likelihood Explanation
Preconditions require the specific sequence: `yield_create` → `DeleteAccount` (before timeout/resume) → account name reuse via `CreateAccount` → deposit → attacker-triggered resume with the old `data_id`. Account-name reuse after deletion is a normal, permissionless NEAR operation (no special privilege needed), and `data_id`s from prior receipts are derivable/observable from chain history, so the attack is fully reachable by an ordinary unprivileged account holder with no validator/node access. The main constraint is that the new account owner must deploy a contract capable of invoking `promise_yield_resume` with a caller-supplied `data_id` (or the resume must otherwise be triggerable), which is plausible for many generic yield/resume-based contracts (marketplaces, oracles, cross-contract call relayers) that don't scope resumption strictly to internally generated IDs.

### Recommendation
`remove_account` should also purge any account-scoped promise-yield state: iterate and remove `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::PromiseYieldTimeout` entries (or lazily invalidate them via an incarnation counter stored per account and included in the trie key/lookup), plus `YieldIdToDataId`/`DataIdToYieldId` mappings, for `account_id`. Alternatively, bind `PromiseYieldReceipt`/`PromiseYieldStatus` keys to an account incarnation nonce that changes on `CreateAccount`, so resumption cannot match state created by a different incarnation of the same-named account.

### Proof of Concept
Runtime/unit test plan:
1. Create account `A`, execute a contract call that performs `yield_create`, capturing the resulting `data_id`.
2. Apply a `DeleteAccount` action for `A` (with a valid beneficiary), and commit the trie update.
3. Assert directly against the trie via `has_promise_yield_receipt(trie, "A".parse().unwrap(), data_id)` and `has_promise_yield_status(trie, &"A".parse().unwrap(), data_id)` — expected result under a correct fix: both return `false`/`None`; current behavior: both return `true`, demonstrating the surviving stale row.
4. Re-create `A` via `CreateAccount`, deposit funds, deploy a new contract exposing a method that calls `promise_yield_resume(data_id, data)`.
5. Invoke that method with the old `data_id`; assert (bug present) that the resume is accepted (`submit_promise_resume_data` returns `true`) and that the stale callback `ActionReceipt`'s actions are dispatched/executed against `A`'s new balance, demonstrating unauthorized value movement out of the re-created account.

### Citations

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

**File:** runtime/runtime/src/ext.rs (L402-426)
```rust
    fn submit_promise_resume_data(
        &mut self,
        data_id: CryptoHash,
        data: Vec<u8>,
    ) -> Result<bool, VMLogicError> {
        let has_yield_receipt_in_state =
            has_promise_yield_receipt(self.trie_update, self.account_id.clone(), data_id)
                .map_err(wrap_storage_error)?;
        let has_yield_status_in_state =
            has_promise_yield_status(self.trie_update, &self.account_id, data_id)
                .map_err(wrap_storage_error)?;

        if has_yield_receipt_in_state || has_yield_status_in_state {
            self.receipt_manager.create_promise_resume_receipt(data_id, data);
            set_promise_yield_status(
                &mut self.trie_update,
                &self.account_id,
                data_id,
                PromiseYieldStatus::ResumeInitiated,
            );
            return Ok(true);
        }

        Ok(false)
    }
```
