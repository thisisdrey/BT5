### Title
Stale `PendingDataCount`/`PostponedReceipt`/`PostponedReceiptId` survive `remove_account`, letting a deleted account's dangling receipt execute attacker-chosen actions against whoever later re-creates the same account name - (File: `core/store/src/utils/mod.rs`)

### Summary
`remove_account` only removes `TrieKey::Account`, `ContractCode`, `AccessKey`/`GasKeyNonce`, and `ContractData` for a deleted account, but never removes `TrieKey::ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, or `PostponedReceipt`. If an account is deleted while it still has an outstanding postponed `ActionReceipt` awaiting a `DataReceipt`, that postponed receipt (with its attacker-chosen actions) remains keyed by the plain `AccountId` string in the trie. When the missing `DataReceipt` finally arrives, the runtime looks the postponed receipt up purely by `AccountId` and executes it against whatever `Account` currently occupies that name - including a brand-new, unrelated owner's account if the name was deleted and later re-registered.

### Finding Description
`remove_account` (`core/store/src/utils/mod.rs:504-575`) is invoked from `action_delete_account` (`runtime/runtime/src/actions.rs:371`) whenever a `DeleteAccountAction` executes. It explicitly removes `Account`, `ContractCode`, all `AccessKey`/gas-key entries, and `ContractData`, but there is no code path that iterates or removes `TrieKey::ReceivedData`, `TrieKey::PostponedReceiptId`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceipt` for the account being deleted: [1](#0-0) 

These four trie keys are all indexed by `receiver_id: AccountId` (confirmed by `TrieKey::get_account_id()`), which is exactly what makes them logically "belong" to the account, yet they're skipped by `remove_account`: [2](#0-1) 

The postponed-receipt lifecycle is: when an `ActionReceipt` targeting account `A` has unresolved `input_data_ids`, the runtime stores `PostponedReceiptId{A, data_id}`, `PendingDataCount{A, receipt_id}`, and `PostponedReceipt{A, receipt_id}`: [3](#0-2) 

Later, when the corresponding `DataReceipt` for `data_id` finally arrives at `A`, `process_receipt` looks up `PostponedReceiptId{A, data_id}` purely by the string `AccountId`, decrements `PendingDataCount{A, receipt_id}`, and once it hits zero, fetches and executes `PostponedReceipt{A, receipt_id}` via `apply_action_receipt` against the account currently stored under `A`: [4](#0-3) 

Nowhere in this lookup/execution path is there any check binding the postponed receipt to the specific "incarnation" of the account (e.g. a creation height/generation counter) — it is purely keyed by the `AccountId` string. `action_create_account` likewise performs no historical-existence check; it only rejects creation if an account currently exists (`account.is_some()`), so a previously-deleted name can be freely re-registered by any predecessor with permission over that namespace: [5](#0-4) [6](#0-5) 

Exploit flow:
1. Attacker's own account `A` submits a transaction that creates an action receipt `R2` (receiver = `A`) with an `input_data_ids` entry that has not yet been satisfied (e.g. via a `promise_then` callback chained off a cross-contract call whose `DataReceipt` has not yet been delivered). This stores `PostponedReceiptId{A,...}`, `PendingDataCount{A,R2}=1`, `PostponedReceipt{A,R2}` containing attacker-chosen actions (e.g. `AddKey` with attacker's public key, or a `Transfer`/`FunctionCall`).
2. Before the missing `DataReceipt` arrives, the attacker submits a `DeleteAccountAction` on `A` (they hold the full-access key, so this is authorized for `A` at that time). `remove_account` deletes the `Account`, keys, code, and contract data, but leaves `PendingDataCount{A,R2}`, `PostponedReceiptId{A,...}`, and `PostponedReceipt{A,R2}` in the trie. The tiny size of these entries never triggers `DeleteAccountWithLargeState` (`MAX_ACCOUNT_DELETION_STORAGE_USAGE`).
3. `A` becomes an available account name again. At some point a different party creates a new account named `A` (top-level names can be freed and re-registered; the same applies to sub-account names once the parent again grants creation).
4. The originally pending `DataReceipt` (whose generation the attacker fully controls the timing of, since it is produced by their own promise chain) finally arrives at `A`. `process_receipt` finds `PostponedReceiptId{A,...}` still present, decrements `PendingDataCount` to 0, fetches `PostponedReceipt{A,R2}`, and calls `apply_action_receipt`, executing the attacker's stored actions against the new owner's live `Account`, keys, and balance.

None of NEAR's existing checks (nonce/signature verification, access-key permission checks, `check_account_existence`, `DeleteAccountWithLargeState`) protect against this because the postponed-receipt execution path is entirely separate from transaction verification — it is triggered purely by trie-state lookups keyed on the `AccountId` string with no binding to a specific account "generation."

### Impact Explanation
This is an authorization-escalation / unexpected-action-execution bug: actions that were authorized only by the original (deleted) account owner get executed against a completely different, later owner's account without their consent, potentially draining funds, adding attacker-controlled access keys, or invoking arbitrary function calls with the new account's identity/balance. This matches the "authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
The attacker needs no privileged access — only an ordinary account and the ability to (a) create a receipt to itself with an outstanding data dependency and (b) delete that account via a normal `DeleteAccountAction`, both of which are standard unprivileged operations. The harder precondition is arranging for the vacated account name to be reused by a genuinely different party while the dangling postponed receipt is still outstanding; this is realistic for coveted/short account names that get squatted, deleted, and re-registered, and the attacker fully controls the timing of when the dependent `DataReceipt` is finally delivered (they can hold off completing their own promise chain until after the name is reused). Even absent third-party reuse, the underlying code defect — `remove_account` leaving `PendingDataCount`/`PostponedReceipt`/`PostponedReceiptId`/`ReceivedData` behind — is deterministic and trivially reproducible.

### Recommendation
`remove_account` should also purge, for the account being deleted, all `ReceivedData`, `PostponedReceiptId`, `PendingDataCount`, and `PostponedReceipt` entries keyed by that `AccountId` (iterating the relevant trie prefixes the same way access keys and contract data are already handled), so that no postponed-receipt state can survive to be replayed against a future account created under the same name.

### Proof of Concept
Runtime/test-loop integration test:
1. Create account `A` with a full access key.
2. From `A`, issue a function call that sets up a `promise_then` callback receipt `R2` (receiver = `A`) depending on the output of a call to a helper contract `B`, without yet delivering `B`'s response (assert `PendingDataCount{A,R2}` and `PostponedReceipt{A,R2}` exist in state via direct trie inspection).
3. Submit a `DeleteAccountAction` from `A` (beneficiary = some other account) and commit it; assert `Account{A}` is gone but `PendingDataCount{A,R2}`/`PostponedReceipt{A,R2}` still present in the trie.
4. Recreate account `A` (via `CreateAccountAction` from an appropriate predecessor) with a fresh access key belonging to a "new owner" and give it a balance.
5. Deliver the originally-pending `DataReceipt` from `B` to `A`.
6. Assert that `apply_action_receipt` executes `R2`'s actions (e.g. observe an `AddKey`/`Transfer` outcome) against the new account `A`, and that the new owner's balance/keys are mutated by actions they never signed — demonstrating unauthorized execution on the re-created account.

### Citations

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

**File:** core/primitives/src/trie_key.rs (L1207-1230)
```rust
            assert_eq!(
                TrieKey::PostponedReceiptId {
                    receiver_id: account_id.clone(),
                    data_id: Default::default()
                }
                .get_account_id(),
                Some(account_id.clone())
            );
            assert_eq!(
                TrieKey::PendingDataCount {
                    receiver_id: account_id.clone(),
                    receipt_id: Default::default()
                }
                .get_account_id(),
                Some(account_id.clone())
            );
            assert_eq!(
                TrieKey::PostponedReceipt {
                    receiver_id: account_id.clone(),
                    receipt_id: Default::default()
                }
                .get_account_id(),
                Some(account_id.clone())
            );
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

**File:** runtime/runtime/src/lib.rs (L1608-1655)
```rust
        let mut pending_data_count: u32 = 0;
        for data_id in action_receipt.input_data_ids() {
            if !has_received_data(state_update, account_id, *data_id)? {
                pending_data_count += 1;
                // The data for a given data_id is not available, so we save a link to this
                // receipt_id for the pending data_id into the state.
                set(
                    state_update,
                    TrieKey::PostponedReceiptId {
                        receiver_id: account_id.clone(),
                        data_id: *data_id,
                    },
                    receipt.receipt_id(),
                )
            }
        }

        if pending_data_count == 0 {
            // All input data is available. Executing the receipt. It will cleanup
            // input data from the state.
            return self
                .apply_action_receipt(
                    state_update,
                    apply_state,
                    pipeline_manager,
                    receipt,
                    receipt_sink,
                    instant_receipts,
                    validator_proposals,
                    stats,
                    epoch_info_provider,
                    receipt_to_tx,
                )
                .map(Some);
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

**File:** runtime/runtime/src/actions.rs (L787-817)
```rust
pub(crate) fn check_account_existence(
    action: &Action,
    account: &Option<Account>,
    account_id: &AccountId,
    config: &RuntimeConfig,
    implicit_account_creation_eligible: bool,
) -> Result<(), ActionError> {
    match action {
        Action::CreateAccount(_) => {
            if account.is_some() {
                return Err(ActionErrorKind::AccountAlreadyExists {
                    account_id: account_id.clone(),
                }
                .into());
            } else {
                if account_is_implicit(account_id, config.wasm_config.eth_implicit_accounts) {
                    // If the account doesn't exist and it's implicit, then you
                    // should only be able to create it using single transfer action.
                    // Because you should not be able to add another access key to the account in
                    // the same transaction.
                    // Otherwise you can hijack an account without having the private key for the
                    // public key. We've decided to make it an invalid transaction to have any other
                    // actions on the implicit hex accounts.
                    // The easiest way is to reject the `CreateAccount` action.
                    // See https://github.com/nearprotocol/NEPs/pull/71
                    return Err(ActionErrorKind::OnlyImplicitAccountCreationAllowed {
                        account_id: account_id.clone(),
                    }
                    .into());
                }
            }
```
