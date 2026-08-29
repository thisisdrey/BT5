### Title
Postponed self-receipt with `AddKeyAction` survives `DeleteAccount` and is replayed against the recreated account, granting the attacker a FullAccess key on the new owner's account - ([File: runtime/runtime/src/actions.rs])

### Summary
`remove_account` (called from `action_delete_account`) only deletes the `Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` trie entries for a deleted account; it never removes `PostponedReceipt`, `PostponedReceiptId`, `PendingDataCount`, or `ReceivedData` entries keyed by that `receiver_id`. Because `check_actor_permissions` for `AddKey` only checks `actor_id == account_id` (a string equality baked into the receipt at creation time, not tied to key/account ownership), a self-addressed postponed `ActionReceipt` containing an `AddKeyAction` that was queued before deletion will still execute successfully — against whichever account now sits at that `account_id` — once its outstanding `DataReceipt` finally arrives.

### Finding Description
`remove_account` in [1](#0-0)  removes `TrieKey::Account`, `TrieKey::ContractCode`, all `AccessKey`/`GasKeyNonce` entries, and `ContractData`, but never touches `TrieKey::PostponedReceipt`, `TrieKey::PostponedReceiptId`, or `TrieKey::PendingDataCount` for the account. `action_delete_account` calls only this function and has no additional check for outstanding postponed receipts before deleting the account [2](#0-1) .

A postponed receipt is created whenever an incoming `ActionReceipt` has unresolved `input_data_ids`: it is stored via `set_postponed_receipt` keyed by `(receiver_id, receipt_id)` [3](#0-2) , together with a `PendingDataCount` and `PostponedReceiptId` link keyed by `(receiver_id, data_id)`.

When the outstanding `DataReceipt` for that account finally arrives, `process_receipt` looks up `PostponedReceiptId`/`PendingDataCount`/`PostponedReceipt` purely by `receiver_id` (the account id string) and, once the count reaches zero, fetches and executes the stored receipt via `apply_action_receipt` [4](#0-3) . Inside `apply_action_receipt`, the account is re-fetched fresh from the *current* trie (`get_account(state_update, account_id)`) and `actor_id` is initialized from the stored receipt's `predecessor_id` [5](#0-4) . For a self-receipt where `predecessor_id == receiver_id == "sub.p.near"`, `actor_id` trivially equals `account_id`, so `check_actor_permissions` for `AddKey` passes unconditionally [6](#0-5)  — regardless of who currently owns `sub.p.near` in the trie.

Exploit flow:
1. Attacker owns `sub.p.near` and deploys a contract on it that issues a self-addressed promise (`Promise::then` on itself) whose callback receipt contains `Action::AddKey(attacker_key, FullAccess)` and has an `input_data_id` that will not resolve immediately (e.g., depends on a cross-shard/cross-contract call). This receipt is stored as a `PostponedReceipt` under `receiver_id = "sub.p.near"`.
2. Attacker (still the account owner) issues `DeleteAccount { beneficiary_id }` from `sub.p.near` to itself; `actor_id == account_id` and `locked == 0`, so `check_actor_permissions`/`DeleteAccountStaking` pass, and `remove_account` runs — leaving the `PostponedReceipt`/`PendingDataCount`/`PostponedReceiptId` rows in the trie untouched.
3. `p.near` (or attacker after regaining control of `p.near`) later issues `CreateAccountAction` for `sub.p.near`, which succeeds because it is a valid sub-account creation by the parent (`action_create_account`, `CreateAccountNotAllowed` check passes) [7](#0-6) .
4. The pending `DataReceipt(data_id=D)` for the original promise eventually arrives (it is independent of account existence and guaranteed to eventually be delivered per the receipt-matching invariant). `process_receipt` finds the stale `PostponedReceiptId`/`PendingDataCount` still present for `sub.p.near`, decrements to zero, fetches the stale `PostponedReceipt`, and executes it via `apply_action_receipt` against the *new* account.
5. `check_actor_permissions` passes trivially (`actor_id == account_id` by construction of the stale self-receipt), and `AddKeyAction` inserts the attacker's `FullAccess` key into the new owner's account.

No existing check re-validates that the account which originally created the postponed receipt still exists/is the same account, and no cleanup step purges stale postponed-receipt state on `DeleteAccount`.

### Impact Explanation
This is a permanent authorization escalation / account-takeover primitive: a third party who legitimately creates `sub.p.near` (funding it with a real balance, deploying a contract, etc.) unknowingly inherits an attacker-controlled `FullAccess` key, giving the attacker full control (theft of funds/contract state) over an account they no longer nominally own. This matches the "theft of account control (permanent takeover)" / authorization-escalation-across-accounts bounty category.

### Likelihood Explanation
The attacker only needs: (1) ownership of a sub-account (cheap, self-funded), (2) ability to deploy a contract to create a genuinely postponed cross-contract callback (standard SDK feature, no privileged access needed), and (3) to trigger `DeleteAccount` on their own account (`self`-permission, always allowed). The final trigger (delivering the delayed `DataReceipt`) is guaranteed by protocol receipt-matching guarantees and can be timed by the attacker (e.g., by controlling the callee contract of the intermediate promise). No validator/node/social-engineering access is required — this is fully reachable from an ordinary funded account submitting transactions/contract calls to public RPC. The precondition that a third party subsequently recreates the exact deleted sub-account name is a real-world constraint but plausible for sub-accounts with recognizable/valuable names, and is entirely under attacker control if the attacker retains control of the parent account.

### Recommendation
`action_delete_account`/`remove_account` should purge all `PostponedReceipt`, `PostponedReceiptId`, `PendingDataCount`, `ReceivedData`, `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`/`DataIdToYieldId` entries for the deleted `account_id` (mirroring what is already done for access keys and contract data), or alternatively fail/refuse deletion while a postponed receipt is outstanding for the account. Additionally, consider binding postponed-receipt authorization to something invariant across account recreation (e.g., a per-account creation nonce/generation counter checked at execution time) so that a stale postponed self-receipt cannot be replayed against a semantically different (recreated) account.

### Proof of Concept
Runtime `apply.rs`-style integration test (analogous to `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code`):
1. Create `parent` and `child = "child.parent.near"`, funded.
2. From `child`, construct and apply a receipt with `predecessor_id == receiver_id == child`, `input_data_ids = [D]`, `actions = [AddKeyAction { attacker_key, FullAccess }]` — with `D` not yet resolved, so it becomes a `PostponedReceipt` (assert `get_postponed_receipt(state, child, receipt_id).is_some()`).
3. Apply a `DeleteAccount { beneficiary_id: parent }` receipt from `child` to itself; assert it succeeds and `get_account(state, child).is_none()`. Assert the `PostponedReceipt`/`PendingDataCount` rows are still present (`get_postponed_receipt` still `Some`).
4. Apply a `CreateAccount` receipt from `parent` to `child` (recreating it), funding it, from a *different* signer/access key representing the new owner.
5. Apply a `DataReceipt { receiver_id: child, data_id: D, data: Some(..) }`.
6. Assert the outcome shows the `AddKeyAction` succeeded and `get_access_key(state, child, attacker_key)` returns `Some(FullAccess)` — proving the attacker's key was injected into the newly-owned, unrelated account.

### Citations

**File:** core/store/src/utils/mod.rs (L120-127)
```rust
pub fn set_postponed_receipt(state_update: &mut TrieUpdate, receipt: &Receipt) {
    assert!(matches!(receipt.versioned_receipt(), VersionedReceiptEnum::Action(_)));
    let key = TrieKey::PostponedReceipt {
        receiver_id: receipt.receiver_id().clone(),
        receipt_id: *receipt.receipt_id(),
    };
    set(state_update, key, receipt);
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

**File:** runtime/runtime/src/actions.rs (L167-210)
```rust
pub(crate) fn action_create_account(
    fee_config: &RuntimeFeesConfig,
    account_creation_config: &AccountCreationConfig,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    account_id: &AccountId,
    predecessor_id: &AccountId,
    result: &mut ActionResult,
) {
    if account_id.is_top_level() {
        if account_id.len() < account_creation_config.min_allowed_top_level_account_length as usize
            && predecessor_id != &account_creation_config.registrar_account_id
        {
            // A short top-level account ID can only be created registrar account.
            result.result = Err(ActionErrorKind::CreateAccountOnlyByRegistrar {
                account_id: account_id.clone(),
                registrar_account_id: account_creation_config.registrar_account_id.clone(),
                predecessor_id: predecessor_id.clone(),
            }
            .into());
            return;
        } else {
            // OK: Valid top-level Account ID
        }
    } else if !account_id.is_sub_account_of(predecessor_id) {
        // The sub-account can only be created by its root account. E.g. `alice.near` only by `near`
        result.result = Err(ActionErrorKind::CreateAccountNotAllowed {
            account_id: account_id.clone(),
            predecessor_id: predecessor_id.clone(),
        }
        .into());
        return;
    } else {
        // OK: Valid sub-account ID by proper predecessor.
    }

    *actor_id = account_id.clone();
    *account = Some(Account::new(
        Balance::ZERO,
        Balance::ZERO,
        AccountContract::None,
        fee_config.storage_usage_config.num_bytes_account,
    ));
}
```

**File:** runtime/runtime/src/actions.rs (L364-389)
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
```

**File:** runtime/runtime/src/actions.rs (L739-760)
```rust
pub(crate) fn check_actor_permissions(
    action: &Action,
    account: &Option<Account>,
    actor_id: &AccountId,
    account_id: &AccountId,
) -> Result<(), ActionError> {
    match action {
        Action::DeployContract(_)
        | Action::Stake(_)
        | Action::AddKey(_)
        | Action::DeleteKey(_)
        | Action::DeployGlobalContract(_)
        | Action::UseGlobalContract(_)
        | Action::WithdrawFromGasKey(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
        }
```

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1396-1455)
```rust
                // given data_id.
                // If we don't have a postponed receipt yet, we don't need to do anything for now.
                if let Some(receipt_id) = get(
                    state_update,
                    &TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    },
                )? {
                    // There is already a receipt that is awaiting for the just received data.
                    // Removing this pending data_id for the receipt from the state.
                    state_update.remove(TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: data_receipt.data_id,
                    });
                    // Checking how many input data items is pending for the receipt.
                    let pending_data_count: u32 = get(
                        state_update,
                        &TrieKey::PendingDataCount { receiver_id: account_id.clone(), receipt_id },
                    )?
                    .ok_or_else(|| {
                        StorageError::StorageInconsistentState(
                            "pending data count should be in the state".to_string(),
                        )
                    })?;
                    if pending_data_count == 1 {
                        // It was the last input data pending for this receipt. We'll cleanup
                        // some receipt related fields from the state and execute the receipt.

                        // Removing pending data count from the state.
                        state_update.remove(TrieKey::PendingDataCount {
                            receiver_id: account_id.clone(),
                            receipt_id,
                        });
                        // Fetching the receipt itself.
                        let ready_receipt =
                            get_postponed_receipt(state_update, account_id, receipt_id)?
                                .ok_or_else(|| {
                                    StorageError::StorageInconsistentState(
                                        "pending receipt should be in the state".to_string(),
                                    )
                                })?;
                        // Removing the receipt from the state.
                        remove_postponed_receipt(state_update, account_id, receipt_id);
                        // Executing the receipt. It will read all the input data and clean it up
                        // from the state.
                        return self
                            .apply_action_receipt(
                                state_update,
                                apply_state,
                                pipeline_manager,
                                &ready_receipt,
                                receipt_sink,
                                instant_receipts,
                                validator_proposals,
                                stats,
                                epoch_info_provider,
                                receipt_to_tx,
                            )
                            .map(Some);
```
