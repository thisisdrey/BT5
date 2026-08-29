### Title
Stale self-authorized `PromiseYieldReceipt` actions (e.g. `AddKey`/`DeployContract`) survive account deletion and execute unauthorized against a recreated account with different owners - ([File: runtime/runtime/src/actions.rs], [File: core/store/src/utils/mod.rs], [File: runtime/runtime/src/lib.rs])

### Summary
`check_actor_permissions` (`runtime/runtime/src/actions.rs:739-785`) authorizes `AddKey`/`DeployContract`/`DeleteKey`/etc. purely by comparing `actor_id == account_id` (an `AccountId` string), with no notion of account "generation"/key continuity. `remove_account` (`core/store/src/utils/mod.rs:505-575`), invoked by `action_delete_account`, purges the `Account`, `ContractCode`, access keys, gas-key nonces and contract data, but never purges `TrieKey::PromiseYieldReceipt` / `PromiseYieldStatus` / `PromiseYieldTimeout` entries keyed by that `account_id`. Because `resolve_promise_yield_timeouts` (`runtime/runtime/src/lib.rs:3009-3105`) fires an automatic, unsigned `PromiseResume` purely from `contains_key(&promise_yield_key)` with no account-existence check, a self-call action list frozen in a dormant `PromiseYieldReceipt` before deletion is executed later against whatever account currently occupies that `account_id`, and `check_actor_permissions` passes trivially since predecessor==receiver by construction.

### Finding Description
1. An attacker who owns `account_id` = X (a top-level name ≥ `min_allowed_top_level_account_length`, requiring no registrar, per `action_create_account`, `runtime/runtime/src/actions.rs:167-200`) calls a contract on X that creates a `PromiseYield` receipt whose action list is a self-call containing `Action::AddKey(attacker_pubkey, FullAccess)` (predecessor_id == receiver_id == X). This is stored via `set_promise_yield_receipt` (`lib.rs:1495-1499`) and a matching `PromiseYieldTimeout` queue entry is scheduled.
2. The attacker deletes account X with `Action::DeleteAccount`. `action_delete_account` (`runtime/runtime/src/actions.rs:314-390`) calls `remove_account` (`core/store/src/utils/mod.rs:505`), which removes `Account`, `ContractCode`, access keys/gas-key nonces, and `ContractData`, but does **not** remove the `PromiseYieldReceipt`, `PromiseYieldStatus`, or pending `PromiseYieldTimeout` entries for X. These remain live in the trie.
3. Some later, unrelated party (e.g. a platform that recycles subaccount names, or anyone who registers the now-freed top-level name X) creates a fresh account at the same `account_id` X, with entirely different keys.
4. Before/at the scheduled timeout height, `resolve_promise_yield_timeouts` (`runtime/runtime/src/lib.rs:3009-3105`) finds `state_update.contains_key(&promise_yield_key)` still true (line 3051) and synthesizes an unsigned `PromiseResume{data: None}` receipt destined for X — with no check that the account still belongs to the original creator, or even that its "generation" matches.
5. When this `PromiseResume` is processed (`lib.rs:1500-1568`), the runtime retrieves and removes the yield receipt and executes it via `apply_action_receipt`. The frozen action list (`Action::AddKey`) runs through `apply_action` → `check_actor_permissions` (`actions.rs:739-785`), which only checks `actor_id != account_id` as strings; since the receipt is a self-call (predecessor_id == receiver_id == X by construction), this check passes unconditionally regardless of who currently controls X.
6. `action_add_key` has no barrier here (it only errors on `AddKeyAlreadyExists` for a colliding public key, which the attacker's key won't hit) so the attacker's `FullAccess` key is silently added to the victim's account — full account takeover / fund theft — without any signature, transaction, or authorization from the new owner.

For the narrower literal question about `DeleteKey`: if the stale action is `Action::DeleteKey(pubkey)` targeting a key the *original* owner controlled, `action_delete_key` looks up that specific public key on the (recreated) account; since the new owner's keys differ, it returns `ActionErrorKind::DeleteKeyDoesNotExist` (`core/primitives/src/errors.rs:749-751`) as a normal action failure — not a silent no-op, and with no state mutation. So `DeleteKey` alone is not exploitable this way. The exploitable gap is specifically for actions like `AddKey`/`DeployContract`/`DeployGlobalContract`/`UseGlobalContract`/`WithdrawFromGasKey` that have no precondition tied to prior key/content identity, and thus execute unconditionally once `check_actor_permissions` is trivially satisfied by the string-based self-call check.

### Impact Explanation
An unprivileged attacker can plant a dormant, unsigned, self-triggering `AddKey(FullAccess)` (or `DeployContract`) bomb on an account name they control, delete that account, and have it fire automatically (via the protocol's own timeout mechanism, no attacker signature required at fire-time) against whoever legitimately reuses that exact account name afterward — granting the attacker a full-access key (or a malicious contract) on the new owner's account. This is authorization escalation across accounts/promises leading to theft of funds, matching the "authorization escalation" / "theft of user funds" NEAR bounty categories.

### Likelihood Explanation
Preconditions: the account name must be genuinely reused by an unrelated party after deletion, and this must occur before the `PromiseYieldTimeout` fires (`resolve_promise_yield_timeouts`, bounded by the configured `yield_timeout_length_in_blocks`). For long top-level names (>`min_allowed_top_level_account_length`) this needs no registrar and is fully attacker-controlled to set up; the harder part is getting a *specific* future owner to recreate the exact same name within the timeout window. The most realistic exploitation is against any platform pattern that programmatically deletes and recycles subaccount names (e.g. `user.<dapp>.near`) shortly after account teardown, since name reuse there is deterministic and fast rather than random. Cost to the attacker is a few cheap transactions (create yield, delete account); it is fully repeatable against any name-recycling scheme.

### Recommendation
- In `action_delete_account` / `remove_account` (`core/store/src/utils/mod.rs:505`), also purge any `TrieKey::PromiseYieldReceipt`, `PromiseYieldStatus`, and pending `PromiseYieldTimeout` entries whose `receiver_id`/`account_id` matches the account being deleted, so no dormant self-authorized receipt can survive account deletion.
- Alternatively/additionally, have `resolve_promise_yield_timeouts` and the `PromiseResume` handling path in `lib.rs` verify the account still exists and matches an identity/generation marker recorded at yield-creation time before re-executing the frozen action list, rather than relying solely on `check_actor_permissions`'s string-based self-call check.

### Proof of Concept
Integration/runtime test-loop plan:
1. Deploy a test contract to account `X` (long top-level name, no registrar needed) that, in one call, creates a `PromiseYield` with a callback action batch of `Action::AddKey(attacker_pubkey, FullAccess)` targeting `X` itself (self-call).
2. In the same or a following transaction, have `X` execute `Action::DeleteAccount` with some beneficiary.
3. Assert via state inspection that `TrieKey::PromiseYieldReceipt{receiver_id: X, data_id}` and the corresponding `PromiseYieldTimeout` queue entry still exist after deletion (gap in `remove_account`).
4. Create account `X` again (fresh `CreateAccountAction` + `AddKey` with a brand-new "victim" key), simulating a different owner.
5. Advance blocks to the scheduled yield timeout height so `resolve_promise_yield_timeouts` synthesizes the `PromiseResume`.
6. Run the apply loop through the resume; assert that `get_access_key(X, attacker_pubkey)` now returns `Some(FullAccess)` on the victim's freshly created account — demonstrating unauthorized key injection.
7. As a control, repeat steps 1-6 with `Action::DeleteKey(attacker_pubkey)` instead of `AddKey`, and assert the result is `ActionErrorKind::DeleteKeyDoesNotExist` with no state mutation, confirming `DeleteKey` itself is not exploitable while `AddKey`/`DeployContract` are. [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) [7](#0-6)

### Citations

**File:** runtime/runtime/src/actions.rs (L167-200)
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
```

**File:** runtime/runtime/src/actions.rs (L314-390)
```rust
pub(crate) fn action_delete_account(
    state_update: &mut TrieUpdate,
    account: &mut Option<Account>,
    actor_id: &mut AccountId,
    receipt: &Receipt,
    result: &mut ActionResult,
    account_id: &AccountId,
    delete_account: &DeleteAccountAction,
    config: &RuntimeConfig,
    current_protocol_version: ProtocolVersion,
) -> Result<(), StorageError> {
    let account_ref = account.as_ref().unwrap();
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

**File:** runtime/runtime/src/actions.rs (L739-785)
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
        Action::DeleteAccount(_) => {
            if actor_id != account_id {
                return Err(ActionErrorKind::ActorNoPermission {
                    account_id: account_id.clone(),
                    actor_id: actor_id.clone(),
                }
                .into());
            }
            let account = account.as_ref().unwrap();
            if !account.locked().is_zero() {
                return Err(ActionErrorKind::DeleteAccountStaking {
                    account_id: account_id.clone(),
                }
                .into());
            }
        }
        Action::CreateAccount(_)
        | Action::FunctionCall(_)
        | Action::Transfer(_)
        | Action::TransferToGasKey(_) => (),
        Action::Delegate(_) | Action::DelegateV2(_) => (),
        Action::DeterministicStateInit(_) => (),
    };
    Ok(())
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

**File:** runtime/runtime/src/lib.rs (L1495-1568)
```rust
            VersionedReceiptEnum::PromiseYield(_) => {
                // Received a new PromiseYield receipt. We simply store it and await
                // the corresponding PromiseResume receipt.
                set_promise_yield_receipt(state_update, receipt);
            }
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

                    // Execute the PromiseYield receipt. It will read the input data and clean it
                    // up from the state.
                    return self
                        .apply_action_receipt(
                            state_update,
                            apply_state,
                            pipeline_manager,
                            &yield_receipt,
                            receipt_sink,
                            instant_receipts,
                            validator_proposals,
                            stats,
                            epoch_info_provider,
                            receipt_to_tx,
                        )
                        .map(Some);
                } else {
                    // If the user happens to call `promise_yield_resume` multiple times, it may so
                    // happen that multiple PromiseResume receipts are delivered. We can safely
                    // ignore all but the first.
                    return Ok(None);
                }
```

**File:** runtime/runtime/src/lib.rs (L3025-3068)
```rust
    while promise_yield_indices.first_index < promise_yield_indices.next_available_index {
        if total.compute >= compute_limit || state_update.trie.check_proof_size_limit_exceed() {
            break;
        }

        let queue_entry_key =
            TrieKey::PromiseYieldTimeout { index: promise_yield_indices.first_index };

        let queue_entry =
            get::<PromiseYieldTimeout>(state_update, &queue_entry_key)?.ok_or_else(|| {
                StorageError::StorageInconsistentState(format!(
                    "PromiseYield timeout queue entry #{} should be in the state",
                    promise_yield_indices.first_index
                ))
            })?;

        // Queue entries are ordered by expires_at
        if queue_entry.expires_at > apply_state.block_height {
            break;
        }

        // Check if the yielded promise still needs to be resolved
        let promise_yield_key = TrieKey::PromiseYieldReceipt {
            receiver_id: queue_entry.account_id.clone(),
            data_id: queue_entry.data_id,
        };
        if state_update.contains_key(&promise_yield_key, AccessOptions::DEFAULT)? {
            let new_receipt_id = create_receipt_id_from_receipt_id(
                &queue_entry.data_id,
                apply_state.block_height,
                new_receipt_index,
            );
            new_receipt_index += 1;

            // Create a PromiseResume receipt to resolve the timed-out yield.
            let resume_receipt = Receipt::V0(ReceiptV0 {
                predecessor_id: queue_entry.account_id.clone(),
                receiver_id: queue_entry.account_id.clone(),
                receipt_id: new_receipt_id,
                receipt: ReceiptEnum::PromiseResume(DataReceipt {
                    data_id: queue_entry.data_id,
                    data: None,
                }),
            });
```

**File:** core/primitives/src/errors.rs (L749-751)
```rust
    /// Account tries to remove an access key that doesn't exist
    DeleteKeyDoesNotExist {
        account_id: AccountId,
```
