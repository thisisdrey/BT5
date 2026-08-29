### Title
Stale receiver-scoped TrieKey state (ReceivedData/PostponedReceipt/PromiseYieldReceipt/PromiseYieldStatus/Yield-Data mappings) survives account deletion, enabling cross-account receipt execution after account recreation - (File: core/store/src/utils/mod.rs -> `remove_account`)

### Summary
`remove_account` in `core/store/src/utils/mod.rs` only clears `TrieKey::Account`, `TrieKey::ContractCode`, the `AccessKey`/`GasKeyNonce` prefix, and the `ContractData` prefix. It leaves the receiver-scoped `TrieKey::ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` entries in place, even though `TrieKey::get_account_id()` (core/primitives/src/trie_key.rs:590-619) reports these as account-scoped data [1](#0-0) . `action_delete_account` (runtime/runtime/src/actions.rs:314-390) calls `remove_account` unconditionally, without checking for outstanding postponed/yielded receipts targeting the account being deleted [2](#0-1) .

### Finding Description
`remove_account` performs exactly four categories of removal: `Account`, `ContractCode`, iterated `AccessKey`/`GasKeyNonce`, and iterated `ContractData` [3](#0-2) . It does not enumerate or clear `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, or `DataIdToYieldId`, all of which `TrieKey::get_account_id` classifies as belonging to the receiver account [4](#0-3) .

`action_delete_account` validates only storage-usage limits and gas-key balance before invoking `remove_account`; it does not check whether the account has any pending postponed receipts or outstanding yields [5](#0-4) . A single `DeleteAccount` action is even fast-pathed as an "instant receipt" when it's the sole action with no input data [6](#0-5) .

Exploit flow (all steps are actions an unprivileged, funded account can perform):
1. Attacker controls account `victim.near`. Attacker arranges (e.g. via a cross-contract call it initiates, or a `promise_yield_create`) for a receipt targeting `victim.near` with unmet `input_data_ids` to be postponed. This causes the runtime to write `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, and `TrieKey::PostponedReceipt` (all keyed by `receiver_id = victim.near`) via `process_action_receipt` (runtime/runtime/src/lib.rs) before the corresponding `Data`/`PromiseResume` receipt has arrived.
2. While the data receipt is still in flight (cross-shard delay is attacker-controllable/predictable), attacker submits `DeleteAccount` for `victim.near`. `action_delete_account` succeeds, `remove_account` clears `Account`, `ContractCode`, `AccessKey`s, `ContractData` — but leaves `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt` untouched.
3. Attacker (or anyone) submits `CreateAccount` for `victim.near` again, installing new keys/contract under the same account id.
4. When the delayed `Data` receipt for the original `data_id` finally arrives, `process_receipt`'s `Data` branch (runtime/runtime/src/lib.rs:1386 onward) looks up `TrieKey::PostponedReceiptId`/`PendingDataCount` keyed only by `receiver_id` (not by any per-incarnation nonce), finds the stale entry, decrements the counter, and once it reaches zero fetches and executes the leftover `PostponedReceipt` — now running against the *new* `victim.near` account (new keys, new contract state, new owner) using inputs/state prepared for the *old* incarnation.

Because `TrieKey`s are content-addressed purely by `account_id` (and `data_id`/`receipt_id`), there is no generation/epoch tag distinguishing "old victim.near" from "new victim.near". The runtime's checks (nonce, access-key permission, signature validation) are all evaluated at the time the original action receipt was constructed and are never re-validated at resume time — the postponed receipt is a pre-validated `Receipt` object that is executed directly via `apply_action_receipt`, bypassing all authorization checks that would apply to a fresh transaction against the new account.

Similarly, `PromiseYieldReceipt`/`PromiseYieldStatus`/`YieldIdToDataId`/`DataIdToYieldId` survive deletion; a subsequent `PromiseResume` for the reused `data_id` (potentially collidable/predictable by the attacker since they control the yield creation) resumes and executes the stale yield receipt against the newly created account (runtime/runtime/src/lib.rs:1514-1562).

### Impact Explanation
This is a concrete authorization-escalation and state-corruption primitive: an attacker can cause action receipts constructed for a deleted account incarnation to execute against a re-created account of the same name, mutating that account's state/funds and executing arbitrary attacker-influenced action lists without the new account owner's consent — this is not merely orphaned-storage garbage, it results in unauthorized action execution against a live account, satisfying "authorization escalation across accounts or promises."

### Likelihood Explanation
Preconditions are within reach of a single unprivileged account: fund an account, trigger a cross-contract call or `promise_yield_create` targeting itself (or have another account call into it) so a postponed/yielded receipt is recorded, then self-delete via `DeleteAccount`, then re-create the same account name (any account can call `CreateAccount` on a name once it no longer exists, subject to normal account-id rules), and wait for/trigger delivery of the outstanding data receipt. The scenario requires precise timing around cross-shard delivery, which is feasible since the attacker controls when it creates the postponed dependency and can predict/influence chunk timing by controlling gas/receipt buffering, and the attack is repeatable at will since it only touches the attacker's own account name.

### Recommendation
Extend `remove_account` in `core/store/src/utils/mod.rs` to also enumerate and remove all `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` entries scoped to the account (via prefix iteration analogous to the existing access-key/contract-data cleanup), or alternatively block `DeleteAccount` when any such pending entries exist for the account (similar to the existing `DeleteAccountStaking`/`GasKeyBalanceTooHigh` guard checks in `action_delete_account`).

### Proof of Concept
Rust unit test plan (in `core/store/src/utils/mod.rs` or a runtime integration test):
1. Populate a `TrieUpdate` with one entry for every `TrieKey` variant where `get_account_id()` returns `Some(account_id)` for a fixed `account_id`.
2. Call `remove_account(&mut state_update, &account_id)`.
3. Iterate the same set of variants and assert `state_update.get(...)` returns `None` for all of them.
4. Expected: this currently fails for `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, `PostponedReceipt`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, `DataIdToYieldId`.

Integration/test-loop PoC (in `test-loop-tests`, modeled on `test_instant_delete_account` in `test-loop-tests/src/tests/create_delete_account.rs`):
1. Create `victim.near`; have it initiate a cross-contract call to itself/another contract producing a postponed receipt with an unmet `input_data_id` (e.g. via `promise_then`/`promise_and` with a slow callback), confirming `TrieKey::PostponedReceipt{receiver_id: victim, ..}` exists via `state_update`/store query before delivering the data.
2. While the data receipt is still pending, submit `DeleteAccount` for `victim.near` with a beneficiary.
3. Submit `CreateAccount` for `victim.near` with new keys.
4. Allow the pending `Data` receipt to be delivered; assert the resumed `PostponedReceipt`'s actions execute and mutate/act on the newly created `victim.near` account (e.g., verify unauthorized state change or fund movement occurs without the new account's key ever authorizing it).

### Citations

**File:** core/primitives/src/trie_key.rs (L590-618)
```rust
    pub fn get_account_id(&self) -> Option<AccountId> {
        match self {
            TrieKey::Account { account_id, .. } => Some(account_id.clone()),
            TrieKey::ContractCode { account_id, .. } => Some(account_id.clone()),
            TrieKey::AccessKey { account_id, .. } => Some(account_id.clone()),
            TrieKey::GasKeyNonce { account_id, .. } => Some(account_id.clone()),
            TrieKey::ReceivedData { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PostponedReceiptId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PendingDataCount { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::PostponedReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DelayedReceiptIndices => None,
            TrieKey::DelayedReceipt { .. } => None,
            TrieKey::ContractData { account_id, .. } => Some(account_id.clone()),
            TrieKey::PromiseYieldIndices => None,
            TrieKey::PromiseYieldTimeout { .. } => None,
            TrieKey::PromiseYieldReceipt { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::BufferedReceiptIndices => None,
            TrieKey::BufferedReceipt { .. } => None,
            TrieKey::BandwidthSchedulerState => None,
            TrieKey::BufferedReceiptGroupsQueueData { .. } => None,
            TrieKey::BufferedReceiptGroupsQueueItem { .. } => None,
            // Even though global contract code might be deployed under account id, it doesn't
            // correspond to the data stored for that account id, so always returning None here.
            TrieKey::GlobalContractCode { .. } => None,
            TrieKey::GlobalContractNonce { .. } => None,
            TrieKey::PromiseYieldStatus { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::YieldIdToDataId { receiver_id, .. } => Some(receiver_id.clone()),
            TrieKey::DataIdToYieldId { receiver_id, .. } => Some(receiver_id.clone()),
        }
```

**File:** runtime/runtime/src/actions.rs (L326-371)
```rust
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
```

**File:** core/store/src/utils/mod.rs (L505-574)
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
```

**File:** core/primitives/src/receipt.rs (L480-486)
```rust
            VersionedReceiptEnum::Action(action_receipt) => {
                // Action receipts containing a single DeleteAccount action and no input
                // promises are instant receipts.
                // Deleting an account is a quick trie operation, it's okay to make it instant.
                matches!(action_receipt.actions(), [Action::DeleteAccount(_)])
                    && action_receipt.input_data_ids().is_empty()
            }
```
