### Title
Authorization escalation via account re-creation after `DeleteAccount` with a pending `PromiseYield` timeout — ([File: core/store/src/utils/mod.rs] / [File: runtime/runtime/src/lib.rs])

### Summary
`promise_yield_create` stores a parked action receipt under `TrieKey::PromiseYieldReceipt { receiver_id, data_id }` and schedules a `TrieKey::PromiseYieldTimeout` entry keyed by `expires_at`. `remove_account` (called by `action_delete_account`) deletes the `Account`, `ContractCode`, access keys, gas keys and contract data for the account, but never removes `PromiseYieldReceipt`/`PromiseYieldStatus`/yield-id-mapping keys tied to that account, even though these keys carry `account_id` in their prefix and are listed in `COLUMNS_WITH_ACCOUNT_ID_IN_KEY`. If the same account name is re-created before the timeout height, `resolve_promise_yield_timeouts` will find the stale `PromiseYieldReceipt` still present and synthesize a `PromiseResume` that causes the parked actions (e.g. `AddKeyAction`) to execute against the new account.

### Finding Description
1. Attacker deploys a contract and calls `promise_yield_create` (self-targeted: `receiver_id == current_account_id`), attaching an `AddKeyAction` (full access key) as the eventual callback action list. This creates:
   - `TrieKey::PromiseYieldReceipt { receiver_id: attacker_account, data_id }` [1](#0-0) 
   - `TrieKey::PromiseYieldTimeout { index }` with `expires_at = block_height + yield_timeout_length_in_blocks` [2](#0-1) 
2. Attacker submits `DeleteAccount` on the same account. `action_delete_account` calls `remove_account`, which removes `Account`, `ContractCode`, access keys/gas keys, and `ContractData` — but does **not** touch `PromiseYieldReceipt`, `PromiseYieldStatus`, or yield-id mapping keys for that account. [3](#0-2) [4](#0-3) 
3. A second party (or the attacker under a different key) creates an account with the identical name before `expires_at` is reached. `check_account_existence` for `CreateAccount` only checks whether the `Account` record exists in state; it has no knowledge of orphaned `PromiseYieldReceipt` entries. [5](#0-4) 
4. Once `apply_state.block_height >= expires_at`, `resolve_promise_yield_timeouts` checks `state_update.contains_key(&TrieKey::PromiseYieldReceipt { receiver_id: queue_entry.account_id, data_id })`, finds the still-present orphaned receipt, and synthesizes a `PromiseResume { data: None }` receipt with `predecessor_id == receiver_id == account_id` destined for the same account. [6](#0-5) 
5. When this `PromiseResume` is processed, `process_receipt` finds the parked `yield_receipt` via `get_promise_yield_receipt`, removes the state markers, and calls `apply_action_receipt` with the parked action list against the account that now belongs to the new owner. [7](#0-6) 
6. Inside `apply_action_receipt`, the actor-permission check for privileged actions like `AddKey` is `actor_id != account_id` where `actor_id` is initialized from `receipt.predecessor_id()`. [8](#0-7) [9](#0-8)  Because the original parked receipt's `predecessor_id` was set to `account_id.clone()` (the original attacker account, self-targeted) — and that string is identical to the current occupant's account id — the check passes trivially even though the current occupant never authorized this action. The check is based purely on string equality of `AccountId`, not on any notion of "same account instance"; account re-creation resets ownership but not this authorization check state, because the check never consulted account identity beyond the name.

### Impact Explanation
This allows an unprivileged attacker to plant a "time bomb" action (e.g., `AddKeyAction` granting a full-access key, or other privileged self-actions such as `DeleteKey`/`Stake`/`DeployContract`) that fires against whoever re-registers the same account name within the yield-timeout window, without that new owner's consent. This is an authorization escalation / account-takeover primitive across account re-creation, matching the "authorization escalation across accounts or promises" bounty category.

### Likelihood Explanation
- Preconditions: the attacker must fully control the timing of `DeleteAccount` and rely on someone else (or a separate identity of their own) re-registering the exact same account name before the yield timeout (`yield_timeout_length_in_blocks`, a bounded, attacker-known window) elapses.
- Cost: cheap — one contract deployment, one `promise_yield_create` call, one `DeleteAccount` transaction. No validator/node privileges needed.
- Feasibility caveat: this requires a *third party* to independently choose to re-create the exact same account name within the timeout window (a race), OR the attacker controls both the deleted and re-created account (in which case there's no privilege escalation against another party — the attacker is just re-authorizing themselves, which is not an issue). Real-world exploitability against an *unwitting* victim depends on predictable/desirable account names being re-registered quickly after deletion, e.g., a marketplace of "vanity" sub-account names, or a service that deterministically re-creates a fixed sub-account name after a user resets/deletes it. This is a plausible but timing-dependent scenario, not a guaranteed race.
- Repeatable: yes, once conditions are met, deterministic given block heights.

### Recommendation
When deleting an account in `action_delete_account`/`remove_account`, also enumerate and remove any `PromiseYieldReceipt`, `PromiseYieldStatus`, `YieldIdToDataId`, and `DataIdToYieldId` entries keyed by that `account_id` (similar to how access keys and contract data are enumerated via prefix iteration), and/or invalidate/skip corresponding entries in the `PromiseYieldTimeout` queue. Alternatively, `resolve_promise_yield_timeouts` (and the `PromiseResume` handling path) should verify that the account has not been deleted and re-created between yield creation and resume (e.g., by storing and comparing an account "creation nonce"/incarnation identifier) before executing the parked actions.

### Proof of Concept
Extend `test-loop-tests/src/tests/yield_timeouts.rs` following the pattern of `test_simple_yield_timeout` [10](#0-9) :
1. Deploy the test contract to account `victim.test0`.
2. Call `promise_yield_create` from `victim.test0` with an attached `AddKeyAction(full_access)` targeting a new public key `K1` controlled by the attacker (self-targeted yield).
3. Submit `DeleteAccount` for `victim.test0` (beneficiary arbitrary) in the next block.
4. Submit `CreateAccount` for `victim.test0` from a different signer (simulating the "new owner"), funding it and adding key `K2` (the legitimate new owner's key) — before `yield_timeout_height()`.
5. Advance blocks to `yield_timeout_height()` and `yield_timeout_height() + 1`.
6. Assert that `victim.test0`'s access key list now contains `K1` (the attacker's key) in addition to/instead of `K2`, i.e., `env.rpc_node().view_access_key(&victim_account, &K1)` succeeds — proving the recreated account gained an access key it never authorized. Also assert `get_yield_data_ids_in_state` / `PromiseYieldReceipt` state confirms the entry survived the `DeleteAccount` in step 3 (using the helper at [11](#0-10) ).

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

**File:** runtime/runtime/src/function_call.rs (L160-169)
```rust
                // If the newly created receipt is a PromiseYield, enqueue a timeout for it
                if receipt.is_promise_yield {
                    enqueue_promise_yield_timeout(
                        state_update,
                        &mut promise_yield_indices,
                        account_id.clone(),
                        receipt.input_data_ids[0],
                        apply_state.block_height
                            + config.wasm_config.limit_config.yield_timeout_length_in_blocks,
                    );
```

**File:** runtime/runtime/src/actions.rs (L371-389)
```rust
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

**File:** runtime/runtime/src/actions.rs (L739-768)
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
```

**File:** runtime/runtime/src/actions.rs (L787-818)
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
        }
```

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1511-1562)
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
```

**File:** runtime/runtime/src/lib.rs (L3046-3068)
```rust
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

**File:** test-loop-tests/src/tests/yield_timeouts.rs (L122-150)
```rust
/// Iterate over all PromiseYieldReceipt entries in the given state and collect their data_ids.
fn get_yield_data_ids_in_state(
    client: &Client,
    state_root: CryptoHash,
    shard_uid: ShardUId,
) -> Vec<CryptoHash> {
    let store = client.chain.chain_store().store();
    let trie_storage = Arc::new(TrieDBStorage::new(store.trie_store(), shard_uid));
    let trie = Trie::new(trie_storage, state_root, None);
    let locked_trie = trie.lock_for_iter();
    let mut iter = locked_trie.iter().unwrap();
    iter.seek_prefix(&[col::PROMISE_YIELD_RECEIPT]).unwrap();

    let mut result = vec![];
    for item in iter {
        let (key, _val) = item.unwrap();
        if !key.starts_with(&[col::PROMISE_YIELD_RECEIPT]) {
            break;
        }

        let account = trie_key_parsers::parse_account_id_from_raw_key(&key).unwrap().unwrap();
        let data_id = CryptoHash(key[(key.len() - 32)..].try_into().unwrap());
        let parsed_key = TrieKey::PromiseYieldReceipt { receiver_id: account, data_id };
        assert_eq!(&key, &parsed_key.to_vec());

        result.push(data_id);
    }
    result
}
```

**File:** test-loop-tests/src/tests/yield_timeouts.rs (L336-384)
```rust
#[test]
fn test_simple_yield_timeout() {
    let (mut env, yield_tx_hash, data_id) = prepare_env_with_yield(vec![], None);
    assert!(next_block_height_after_setup() < yield_timeout_height());

    // Advance through the blocks during which the yield will await resumption
    for block_height in next_block_height_after_setup()..yield_timeout_height() {
        env.validator_runner().run_until_head_height(block_height);

        // The transaction will not have a result until the timeout is reached
        assert_eq!(
            env.validator()
                .client()
                .chain
                .get_partial_transaction_result(&yield_tx_hash)
                .unwrap()
                .status,
            FinalExecutionStatus::Started
        );
    }

    // When this block executes, the timeout is processed, producing a YieldResume receipt.
    env.validator_runner().run_until_executed_height(yield_timeout_height());
    // Checks that the anticipated YieldResume receipt was produced.
    assert_eq!(find_yield_data_ids_from_latest_block(&env), vec![data_id]);
    assert_eq!(
        env.validator()
            .client()
            .chain
            .get_partial_transaction_result(&yield_tx_hash)
            .unwrap()
            .status,
        FinalExecutionStatus::Started
    );

    // When this block executes, the resume receipt is applied and the callback will execute.
    env.validator_runner().run_until_executed_height(yield_timeout_height() + 1);
    assert_eq!(
        env.validator()
            .client()
            .chain
            .get_partial_transaction_result(&yield_tx_hash)
            .unwrap()
            .status,
        FinalExecutionStatus::SuccessValue(vec![0u8]),
    );

    assert_no_promise_yield_status_in_state(&env);
}
```
