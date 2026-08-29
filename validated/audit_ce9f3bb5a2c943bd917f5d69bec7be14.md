### Title
Account deletion leaves stale `PromiseYieldReceipt`/`PromiseYieldStatus` entries, letting a resumed promise execute a pre-planted receipt (e.g. `AddKey`) against a later, unrelated re-creation of the same `AccountId` - (File: `core/store/src/utils/mod.rs`, `runtime/runtime/src/ext.rs`)

### Summary
`remove_account` deletes an `Account`, its code, access keys, gas-key nonces and contract data, but never removes the account's `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::PromiseYieldTimeout`, or `YieldIdToDataId`/`DataIdToYieldId` entries. `submit_promise_resume_data` only checks that a row for `(account_id, data_id)` still exists — it has no notion of "current account generation" — so a stale yield created before deletion can still be resumed after the account has been deleted and a brand-new account with the same `AccountId` has been created.

### Finding Description
`remove_account` explicitly enumerates and deletes `Account`, `ContractCode`, access keys, gas-key nonces, and `ContractData`: [1](#0-0) [2](#0-1) 

It never touches `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, the `PromiseYieldTimeout` queue entry, or the `YieldIdToDataId`/`DataIdToYieldId` mappings, all of which are also keyed by `account_id` (as `receiver_id`): [3](#0-2) 

`action_delete_account` calls `remove_account` unconditionally on `DeleteAccount`; it only checks storage usage and locked balance, neither of which accounts for outstanding promise-yield state: [4](#0-3) 

`submit_promise_resume_data` (backing the `promise_yield_resume` host function) authorizes a resume purely by existence of a stale row for `(self.account_id, data_id)`: [5](#0-4) 

When the resulting `PromiseResume` receipt is processed, the runtime looks up the stored yield receipt by `(account_id, data_id)` only, and if found, executes its actions with the attacker-supplied resume payload as the promise result — with no check that the receipt still belongs to the "current" incarnation of the account: [6](#0-5) 

Exploit flow:
1. Attacker creates/controls account `X` (e.g. a permissionlessly-creatable long top-level account, or any sub-account they control) and calls a contract method on `X` that invokes `promise_yield_create`, producing a self-directed `PromiseYield` receipt whose baked-in actions include something high-value, e.g. `Action::AddKey` with an attacker-chosen full-access public key, or a `Transfer`. This is stored via `set_promise_yield_receipt` at `TrieKey::PromiseYieldReceipt{receiver_id: X, data_id: D}` [7](#0-6) , with status `Yielded`.
2. Attacker deletes `X` via `DeleteAccount`. `remove_account` wipes the account/keys/contract but leaves the `(X, D)` promise-yield rows in the trie.
3. `X` is later re-created (by anyone able to claim that `AccountId` again, e.g. a different unrelated party once the name is free) with entirely different keys/contract — a new "generation" of the account.
4. Because `D` was previously visible (data ids are embedded in/derivable from receipt IDs and observable via RPC), attacker calls `promise_yield_resume(D, payload)`. `submit_promise_resume_data` finds `has_promise_yield_receipt(X, D) == true` (the stale row) and happily creates a `PromiseResume` receipt, with no check that this generation of `X` ever created that yield.
5. The runtime finds the stale `PromiseYieldReceipt` and executes its pre-planted actions (e.g. `AddKey` full access) against the new account `X`, since `actor_id`/`account_id` checks in `check_actor_permissions` only verify predecessor==receiver (both are `X` for a self-directed yield receipt), not that the acting principal is the entity that currently controls `X`.

No existing check (signature, nonce, access-key permission, or the resume/`has_promise_yield_*` checks) verifies that the promise-yield row belongs to the account's current lifetime; the only "authorization" is the mere presence of a trie key, which `remove_account` fails to clear.

### Impact Explanation
Authorization escalation across account generations: an attacker can smuggle a pre-planted action (most severely `AddKey` with full access, but also `Transfer`/`FunctionCall`) into whatever account later reuses the same `AccountId`, executing it against a completely different owner's keys/contract state, matching the NEAR bounty category "authorization escalation across accounts or promises."

### Likelihood Explanation
- Root-cause defect (`remove_account` not cleaning promise-yield/queue state) is unconditionally reachable by any unprivileged user who can delete an account they control after first creating a yield — no special privileges are required for steps 1–2.
- The impactful step (steps 3–5, where a *different* party ends up owning the reused `AccountId`) requires the same `AccountId` to be reclaimed after deletion, which is protocol-permitted (e.g. long top-level accounts are permissionlessly creatable by anyone under the root `near` account per `action_create_account`) but depends on someone independently registering exactly that name after the attacker abandons it — this is a real but opportunistic/"account squatting" precondition rather than a fully attacker-controlled trigger.
- The self-directed variant (attacker deletes and immediately recreates their own account to bypass the stale-state cleanup) does not create a cross-party victim and is of lower severity, but still demonstrates the missing invariant.
- Cost to the attacker is minimal: normal transaction fees plus account creation/deletion costs.

### Recommendation
`remove_account` should also purge all `PromiseYield`-related state scoped to the account before/at deletion: iterate and remove any `TrieKey::PromiseYieldReceipt`, `TrieKey::PromiseYieldStatus`, `TrieKey::YieldIdToDataId`/`DataIdToYieldId` entries for `account_id`, and either eagerly cancel corresponding `PromiseYieldTimeout` queue entries or make `resolve_promise_yield_timeouts`/`submit_promise_resume_data`/the `PromiseResume` handler tolerant of a missing account (e.g. only allow resume if `get_account(account_id)` still exists and matches whatever "generation" indicator is stored alongside the yield). At minimum, block `DeleteAccount` while the account has any outstanding `PromiseYieldReceipt`/`PromiseYieldStatus` rows, mirroring the existing storage-usage/locked-balance checks in `action_delete_account`.

### Proof of Concept
Unit test plan (runtime/runtime crate, using `TestTriesBuilder`/`TrieUpdate` similar to `core/store/src/utils/mod.rs`/`runtime/runtime/src/actions.rs` test modules):
1. Set up a `TrieUpdate`, create account `X` with an account row.
2. Call `set_promise_yield_receipt` and `set_promise_yield_status(..., PromiseYieldStatus::Yielded)` for `(X, D)` with a receipt whose actions include `Action::AddKey` for a test public key.
3. Call `remove_account(&mut state_update, &X)`.
4. Assert (this should fail pre-fix, demonstrating the bug): `has_promise_yield_receipt(&state_update, X.clone(), D)? == true` and `has_promise_yield_status(&state_update, &X, D)? == true` even though the account no longer exists (`get_account(&state_update, &X)? == None`).
5. Re-create account `X` with a fresh `Account`/new access key.
6. Construct a `RuntimeExt` for `X` and call `submit_promise_resume_data(D, attacker_payload)`; assert it returns `Ok(true)` and enqueues a `PromiseResume` receipt for `D`.
7. (Integration/test-loop level, extending `test-loop-tests/src/tests/yield_resume.rs` style tests) drive the resulting `PromiseResume` receipt through `process_receipt`/`apply_action_receipt` and assert that the stale `AddKey` action executes and installs the attacker's key on the re-created account `X`, despite `X`'s legitimate owner never authorizing it.

### Citations

**File:** core/store/src/utils/mod.rs (L200-279)
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

pub fn remove_promise_yield_receipt(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::PromiseYieldReceipt { receiver_id: receiver_id.clone(), data_id });
}

pub fn get_promise_yield_receipt(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<Receipt>, StorageError> {
    get(trie, &TrieKey::PromiseYieldReceipt { receiver_id: receiver_id.clone(), data_id })
}

pub fn has_promise_yield_receipt(
    trie: &dyn TrieAccess,
    receiver_id: AccountId,
    data_id: CryptoHash,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::PromiseYieldReceipt { receiver_id, data_id },
        AccessOptions::DEFAULT,
    )
}

pub fn get_promise_yield_status(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<Option<PromiseYieldStatus>, StorageError> {
    get(trie, &TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id })
}

pub fn has_promise_yield_status(
    trie: &dyn TrieAccess,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) -> Result<bool, StorageError> {
    trie.contains_key(
        &TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id },
        AccessOptions::DEFAULT,
    )
}

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

pub fn remove_promise_yield_status(
    state_update: &mut TrieUpdate,
    receiver_id: &AccountId,
    data_id: CryptoHash,
) {
    state_update.remove(TrieKey::PromiseYieldStatus { receiver_id: receiver_id.clone(), data_id });
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

**File:** core/store/src/utils/mod.rs (L555-574)
```rust
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

**File:** runtime/runtime/src/lib.rs (L1500-1568)
```rust
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
