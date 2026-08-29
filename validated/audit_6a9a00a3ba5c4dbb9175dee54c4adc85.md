### Title
Stale postponed `DeleteAccountAction` receipt drains a recreated account's funds - (`runtime/runtime/src/actions.rs::action_delete_account`, `core/store/src/utils/mod.rs::remove_account`)

### Finding Description
When an `ActionReceipt` targeting account `A` has unresolved `input_data_ids`, `process_action_receipt` stores it as a *postponed receipt* keyed only by `(receiver_id, receipt_id)`, together with a `PendingDataCount` and `PostponedReceiptId → receipt_id` link keyed by `(receiver_id, data_id)`: [1](#0-0) 

None of these keys are tied to the *existence* of account `A` — they simply persist in the trie under `A`'s account id. When `A` is deleted via `action_delete_account` → `remove_account`, only the `Account`, `ContractCode`, access keys/gas-key nonces, and contract data are wiped: [2](#0-1) 

`remove_account` never touches `TrieKey::PostponedReceipt`, `TrieKey::PendingDataCount`, or `TrieKey::PostponedReceiptId`. So an attacker-controlled self-receipt to `A` that carries `Action::DeleteAccount(DeleteAccountAction { beneficiary_id: attacker2 })` with an unresolved data dependency remains parked in state across the account's deletion.

Later, when a third party issues a `CreateAccount` + `Transfer` (funding) receipt that recreates `A` (e.g. via a subaccount-creation flow), and then the still-outstanding `DataReceipt` finally arrives, `process_receipt`'s data-receipt branch decrements `PendingDataCount` and, once it reaches 0, fetches and executes the stale postponed receipt without any check that the account is the same account instance that originally created it: [3](#0-2) 

`apply_action_receipt` then loads the account fresh at execution time: [4](#0-3) 

For the stale `DeleteAccount` action, `check_account_existence` passes because the recreated account exists, and `check_actor_permissions` passes because `actor_id` starts as `receipt.predecessor_id()`, which is `A` itself (the original self-receipt), matching `account_id == A`, and the freshly recreated account has `locked() == 0`: [5](#0-4) 

`action_delete_account` then refunds the *new* account's current balance to the attacker's chosen `beneficiary_id` and deletes it, all without the new owner ever signing a transaction: [6](#0-5) 

The repository already contains an analogous test that specifically exercises "delete A, then recreate A in the same/later processing, then a later receipt targeting A resolves against the fresh account" for `FunctionCall`, confirming this delete/recreate resolution pattern is a recognized and tested code path — but the equivalent `DeleteAccount`-in-postponed-receipt beneficiary-theft case is not covered: [7](#0-6) 

### Impact Explanation
Theft of funds: any account name that is deleted while it still has such a stale parked receipt becomes a "trap." If any third party (a registrar contract, faucet, dApp onboarding flow, or another user) later creates and funds an account with that exact name, its full balance is silently transferred to an account chosen entirely by the original attacker, and the new account is destroyed — with no signature or action from the new owner. This matches the NEAR bounty category of theft of funds / value-conservation-adjacent authorization bypass (a receipt authorized by the old occupant of an account id is treated as authorized for a completely different, later occupant of the same id).

### Likelihood Explanation
- Preconditions: attacker fully controls account `A` and its contract; attacker's `DeleteAccount` receipt to itself must have `locked() == 0` (trivial for a non-validator account) and must be constructed to depend on data that doesn't arrive until after `A` is deleted and recreated (achievable via a cross-contract callback whose data receipt is deliberately delayed, or is delivered on demand by the attacker's own downstream contract).
- Cost: only ordinary transaction/gas fees; no validator or node privileges required.
- Feasibility: requires that some third party recreate an account with the exact same name `A` after the attacker vacates it. This is realistic in registrar/subaccount-creation flows (e.g., `name.app.near` factories) where the attacker can front-run/reserve then release a desired subaccount name before a victim's creation request lands, or in any workflow that reuses previously-deleted account names.
- Repeatability: the attacker can pre-plant such traps on many candidate account names cheaply, since the malicious postponed state persists indefinitely in the trie until its data dependency is resolved.

### Recommendation
When an account is deleted (`remove_account`), also purge any state tied to it that could later be resurrected for an unrelated, differently-owned instance of the same account id:
- Enumerate and remove `PostponedReceipt`, `PendingDataCount`, and `PostponedReceiptId` entries (and `ReceivedData`) for that `account_id`, or
- Tag postponed receipts/pending-data-count entries with a per-account "generation"/creation nonce that is checked before executing a postponed receipt, invalidating (and refunding/burning) any postponed receipt whose recorded generation doesn't match the current account's generation.
Either fix ensures a receipt authorized against one incarnation of an account id can never execute against a later, unrelated incarnation of the same id.

### Proof of Concept
Runtime integration test (analogous to `test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code`), extended across chunks:
1. Set up accounts `attacker` (A, no code needed) and `victim_registrar` (third party) and `attacker2` (beneficiary), with `A.locked() == 0`.
2. Build receipt `R_postponed`: `predecessor_id = A`, `receiver_id = A`, `input_data_ids = [some_data_id]`, `actions = [DeleteAccount { beneficiary_id: attacker2 }]`. Apply it in a chunk — assert it is stored as a postponed receipt (not yet executed), i.e. `A`'s account and `A`'s balance are unchanged.
3. In the same/next chunk, apply an instant self-`DeleteAccount` receipt for `A` with `beneficiary_id = attacker` (or anyone) — assert `A`'s `Account` record and access keys are gone via `get_account`.
4. In a later chunk, apply `CreateAccount` + `Transfer(amount)` receipt from `victim_registrar` recreating `A` with a fresh balance `amount`. Assert `get_account(A)` now returns the new account with `amount`.
5. Deliver the `DataReceipt` for `some_data_id` targeting `A`. Assert:
   - The postponed `R_postponed` executes (its `DeleteAccount` action runs against the recreated account).
   - `attacker2`'s balance increases by exactly the new `A`'s `amount`.
   - `get_account(A)` returns `None` afterward, despite `victim_registrar`/the new owner never submitting a `DeleteAccount` transaction.

### Citations

**File:** runtime/runtime/src/lib.rs (L853-856)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
        let mut actor_id = receipt.predecessor_id().clone();
        let mut result = ActionReceiptResult::new();
```

**File:** runtime/runtime/src/lib.rs (L1398-1455)
```rust
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

**File:** runtime/runtime/src/actions.rs (L364-390)
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
}
```

**File:** runtime/runtime/src/actions.rs (L761-776)
```rust
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
```

**File:** runtime/runtime/src/tests/apply.rs (L4877-4982)
```rust
// A FunctionCall whose receiver is deleted and recreated within the same chunk must
// resolve to the freshly recreated (no-code) account, not to a stale contract that
// `ReceiptPreparationPipeline` compiled against the receiver's code as resolved at
// preparation time.
#[test]
fn test_function_call_after_same_chunk_delete_recreate_resolves_fresh_code() {
    let parent = alice_account();
    let child: AccountId = "child.alice.near".parse().unwrap();
    // initial_locked must be 0 so the self-DeleteAccount receipt below passes the
    // DeleteAccountStaking check in `check_actor_permissions`.
    let (runtime, tries, root, mut apply_state, signers, epoch_info_provider) = setup_runtime(
        vec![parent.clone(), child.clone()],
        Balance::from_near(1_000_000),
        Balance::ZERO,
        Gas::from_teragas(1000),
    );
    let parent_signer = signers[0].clone();
    let child_signer = signers[1].clone();

    let deploy = create_receipt_with_actions(
        child.clone(),
        child_signer.clone(),
        vec![Action::DeployContract(DeployContractAction {
            code: near_test_contracts::trivial_contract().to_vec(),
        })],
    );
    let deploy_result = runtime
        .apply(
            tries.get_trie_for_shard(ShardUId::single_shard(), root),
            &None,
            &apply_state,
            &[deploy],
            SignedValidPeriodTransactions::empty(),
            &epoch_info_provider,
            Default::default(),
        )
        .unwrap();
    let root =
        commit_apply_result(&deploy_result, &mut apply_state, &tries, ShardUId::single_shard());
    apply_state.block_height += 1;

    let receipt_gas_price = GAS_PRICE.max(apply_state.config.min_gas_purchase_price);
    let build_receipt = |tag: &str, predecessor: AccountId, signer: &Signer, actions| -> Receipt {
        Receipt::V0(ReceiptV0 {
            predecessor_id: predecessor.clone(),
            receiver_id: child.clone(),
            receipt_id: CryptoHash::hash_borsh((tag, &child)),
            receipt: ReceiptEnum::Action(ActionReceipt {
                signer_id: predecessor,
                signer_public_key: signer.public_key(),
                gas_price: receipt_gas_price,
                output_data_receivers: vec![],
                input_data_ids: vec![],
                actions,
            }),
        })
    };
    let delete = build_receipt(
        "delete",
        child.clone(),
        &child_signer,
        vec![Action::DeleteAccount(DeleteAccountAction { beneficiary_id: parent.clone() })],
    );
    let create_and_call = build_receipt(
        "create_and_call",
        parent,
        &parent_signer,
        vec![
            Action::CreateAccount(CreateAccountAction {}),
            Action::FunctionCall(Box::new(FunctionCallAction {
                method_name: "main".to_string(),
                args: vec![],
                gas: Gas::from_teragas(10),
                deposit: Balance::ZERO,
            })),
        ],
    );
    let call_id = *create_and_call.receipt_id();

    let result = runtime
        .apply(
            tries.get_trie_for_shard(ShardUId::single_shard(), root),
            &None,
            &apply_state,
            &[delete, create_and_call],
            SignedValidPeriodTransactions::empty(),
            &epoch_info_provider,
            Default::default(),
        )
        .unwrap();

    let call_outcome = result
        .outcomes
        .iter()
        .find(|outcome| outcome.id == call_id)
        .expect("function call outcome missing");
    assert_matches!(
        &call_outcome.outcome.status,
        ExecutionStatus::Failure(TxExecutionError::ActionError(ActionError {
            kind: ActionErrorKind::FunctionCallError(FunctionCallError::CompilationError(
                CompilationError::CodeDoesNotExist { .. }
            )),
            ..
        }))
    );
}
```
