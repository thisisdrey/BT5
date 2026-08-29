This confirms the atomicity claim in the question is false. I have sufficient evidence to answer.

### Title
No vulnerability: per-receipt storage proof limit failure fully rolls back the receipt's nonce update - (File: runtime/runtime/src/lib.rs)

### Summary
The premise that a nonce increment from a `DelegateAction` could persist in committed trie changes while the same receipt's later actions fail the hard `PerReceiptStorageProofSizeLimit` is contradicted by the actual commit/rollback logic in `apply_action_receipt`. Any action-level error (including `ActionErrorKind::ReceiptStorageProofSizeExceeded`) causes the *entire* receipt's `TrieUpdate` prospective changes—including the access-key nonce write from `validate_delegate_action_key`—to be discarded via `state_update.rollback()`, not partially committed.

### Finding Description
`validate_delegate_action_key` writes the incremented nonce into the same `state_update: &mut TrieUpdate` that the rest of the receipt's actions use [1](#0-0) . All actions within one action receipt are executed inside `apply_action_receipt`'s per-action loop over `action_receipt.actions()`, which runs on the *same* `state_update` and only calls `state_update.commit(...)` once, at the very end, after every action has succeeded [2](#0-1) . If any action in that loop fails—including a later action tripping `ActionErrorKind::ReceiptStorageProofSizeExceeded` from the per-receipt storage-proof accounting at lines 928-945 [3](#0-2) —the loop breaks with `result.result` as `Err`, and the top-level commit/rollback dispatch takes the `Err` branch, calling `state_update.rollback()` [4](#0-3) . `TrieUpdate::rollback` clears `self.prospective`, i.e., all uncommitted changes made during that receipt's execution, including the nonce write [5](#0-4) . This is documented explicitly: "a receipt whose result is `Err` triggers `state_update.rollback()`, so no state changes persist except the outcome/gas accounting" [6](#0-5) , and in the runtime's own architecture notes: "When a receipt fails, its state changes are rolled back using `TrieUpdate`" [7](#0-6) .

Additionally, the scenario's "Promise-based fan-out sub-receipt" framing does not match how NEAR receipts work: actions that spawn new promises create *new, separate* receipts (`result.new_receipts`) that are queued for later/independent execution (possibly on other shards or in later chunks), not additional actions executed within the *same* top-level receipt's `apply_action_receipt` call. `ActionReceiptResult::set_error` additionally clears any `new_receipts` already queued by earlier successful actions in the same receipt, preventing any child receipts from escaping a failed parent either [8](#0-7) . The `main_storage_proof_size_soft_limit` (checked between whole top-level receipts, per the docs cited in the question) and `PerReceiptStorageProofSizeLimit` (checked within one receipt's action loop) operate at different granularities and neither creates a path for partial-commit of one receipt's state.

There is a documented test confirming exactly this atomicity: `test_add_keys_after_large_read_exceed_receipt_storage_proof_limit` verifies that when a receipt fails with `ReceiptStorageProofSizeExceeded` on a later `AddKey` action, the added keys are *not* present in the committed state afterward [9](#0-8) . This is the general case that also applies to a `DelegateAction`'s nonce write.

### Impact Explanation
No impact. The claimed nonce-persistence-despite-receipt-failure and cross-producer replay scenario does not occur, because the runtime always rolls back all `TrieUpdate` prospective changes (nonce writes included) for a receipt whose action execution results in `Err`, whether that error is `ReceiptStorageProofSizeExceeded` or any other `ActionError`. There is no partial-commit path in `apply_action_receipt`.

### Likelihood Explanation
Not applicable — the described precondition (durable nonce persistence from a failed receipt) cannot be produced by an attacker because commit/rollback is receipt-atomic and unconditional.

### Recommendation
No fix needed; existing atomic commit/rollback semantics in `apply_action_receipt` already prevent this class of issue.

### Proof of Concept
An equivalent PoC already exists in the codebase: `test_add_keys_after_large_read_exceed_receipt_storage_proof_limit` in `runtime/runtime/src/tests/apply.rs` demonstrates that a receipt failing the hard `per_receipt_storage_proof_size_limit` on a later action fully reverts state changes (including access-key related writes) from earlier actions in that same receipt, with no partial-commit path [10](#0-9) .

### Citations

**File:** runtime/runtime/src/actions.rs (L713-727)
```rust
    match nonce_update {
        DelegateNonceUpdate::AccessKey => {
            access_key.nonce = delegate_nonce.nonce();
            set_access_key(state_update, sender_id.clone(), public_key.clone(), &access_key);
        }
        DelegateNonceUpdate::GasKey { nonce_index } => {
            set_gas_key_nonce(
                state_update,
                sender_id.clone(),
                public_key.clone(),
                nonce_index,
                delegate_nonce.nonce(),
            );
        }
    }
```

**File:** runtime/runtime/src/lib.rs (L482-493)
```rust
    /// Marks the receipt as failed: records the error and discards any
    /// receipt-scoped state that would otherwise leak across the failure
    /// boundary (queued receipts, proposed validators, burnt/subsidized
    /// balances). Profile, gas counters, logs and `current_contracts` are
    /// kept — they reflect work already done.
    pub fn set_error(&mut self, err: ActionError) {
        self.result = Err(err);
        self.new_receipts.clear();
        self.validator_proposals.clear();
        self.tokens_burnt = Balance::ZERO;
        self.subsidized_amount = Balance::ZERO;
    }
```

**File:** runtime/runtime/src/lib.rs (L928-945)
```rust
                if let (true, Some(size_before), Some(limit)) = (
                    result.result.is_ok(),
                    storage_proof_size_before_receipt,
                    storage_proof_limit_for_all_actions,
                ) {
                    let recorded_by_receipt = state_update
                        .trie
                        .recorded_storage_size_upper_bound()
                        .saturating_sub(size_before);
                    if recorded_by_receipt > limit {
                        result.set_error(
                            ActionErrorKind::ReceiptStorageProofSizeExceeded {
                                limit: limit as u64,
                            }
                            .into(),
                        );
                    }
                }
```

**File:** runtime/runtime/src/lib.rs (L1024-1034)
```rust
        // Committing or rolling back state.
        match &result.result {
            Ok(_) => {
                state_update.commit(StateChangeCause::ReceiptProcessing {
                    receipt_hash: receipt.get_hash(),
                });
            }
            Err(_) => {
                state_update.rollback();
            }
        };
```

**File:** core/store/src/trie/update.rs (L225-228)
```rust
    pub fn rollback(&mut self) {
        self.prospective.clear();
        self.contract_storage.rollback_deploys();
    }
```

**File:** protocol-model/spec/runtime-execution.md (L149-149)
```markdown
- **Failed receipt atomicity**: a receipt whose result is `Err` triggers `state_update.rollback()`, so no state changes persist except the outcome/gas accounting (`runtime/runtime/src/lib.rs:967`). `set_error` additionally clears queued receipts, proposals, and burnt/subsidized amounts (`runtime/runtime/src/lib.rs:487`).
```

**File:** runtime/runtime/AGENTS.md (L64-64)
```markdown
To modify the shard's state, the runtime uses `TrieUpdate`. This struct applies changes on top of the chunk's pre-state. It allows to rollback or commit recent changes. When a receipt fails, its state changes are rolled back using `TrieUpdate`.
```

**File:** runtime/runtime/src/tests/apply.rs (L1694-1833)
```rust
#[test]
fn test_add_keys_after_large_read_exceed_receipt_storage_proof_limit() {
    const NUM_VALUES: u8 = 4;
    // Part of the limit the values leave free. The read's trie nodes fit in it; the
    // `AddKey` actions that follow do not.
    const RESERVED_UNDER_LIMIT: usize = 1_000;
    // Keys `alice` already holds, enough to fill one branch of the access key subtree. An
    // `AddKey` records the subtree nodes its lookup walks that no earlier one did.
    const NUM_EXISTING_KEYS: usize = 16;
    const NUM_ADDED_KEYS: usize = 6;
    const ACTION_GAS: Gas = Gas::from_teragas(100);

    assert!(ProtocolFeature::EnforceStorageProofLimitForAllActions.enabled(PROTOCOL_VERSION));
    let feature_version = ProtocolFeature::EnforceStorageProofLimitForAllActions.protocol_version();

    let shard_uid = ShardUId::single_shard();
    let existing_signers = (0..NUM_EXISTING_KEYS)
        .map(|i| {
            Arc::new(InMemorySigner::from_seed(
                alice_account(),
                KeyType::ED25519,
                &format!("existing{i}"),
            ))
        })
        .collect();
    let (runtime, tries, root, mut apply_state, signers, epoch_info_provider) =
        setup_runtime_with_keys(
            vec![(alice_account(), existing_signers)],
            Balance::from_near(1_000_000),
            Balance::ZERO,
            Gas::from_teragas(1_000),
        );
    let account = alice_account();
    let signer = signers[0].clone();
    let limit = apply_state.config.wasm_config.limit_config.per_receipt_storage_proof_size_limit;
    let value_size = (limit - RESERVED_UNDER_LIMIT) / NUM_VALUES as usize;

    let apply_receipt = |apply_state: &ApplyState, root: CryptoHash, receipt: &Receipt| {
        let apply_result = runtime
            .apply(
                tries.get_trie_for_shard(shard_uid, root).recording_reads_new_recorder(),
                &None,
                apply_state,
                std::slice::from_ref(receipt),
                SignedValidPeriodTransactions::empty(),
                &epoch_info_provider,
                Default::default(),
            )
            .unwrap();
        let status = apply_result
            .outcomes
            .iter()
            .find(|outcome| outcome.id == *receipt.receipt_id())
            .expect("receipt outcome should be present")
            .outcome
            .status
            .clone();
        (apply_result, status)
    };

    // Setup: deploy the contract, then write the values the receipt reads back.
    let setup_receipt = create_receipt_with_actions(
        account.clone(),
        signer.clone(),
        std::iter::once(Action::DeployContract(DeployContractAction {
            code: near_test_contracts::rs_contract().to_vec(),
        }))
        .chain((0..NUM_VALUES).map(|key| {
            let mut args = vec![key];
            args.extend_from_slice(&(value_size as u32).to_le_bytes());
            Action::FunctionCall(Box::new(FunctionCallAction {
                method_name: "write_value_of_size".to_string(),
                args,
                gas: ACTION_GAS,
                deposit: Balance::ZERO,
            }))
        }))
        .collect(),
    );
    let (apply_result, setup_status) = apply_receipt(&apply_state, root, &setup_receipt);
    assert_matches!(setup_status, ExecutionStatus::SuccessValue(_));
    let root = commit_apply_result(&apply_result, &mut apply_state, &tries, shard_uid);

    let read_action = Action::FunctionCall(Box::new(FunctionCallAction {
        method_name: "read_values_in_key_range".to_string(),
        args: vec![0, NUM_VALUES],
        gas: ACTION_GAS,
        deposit: Balance::ZERO,
    }));
    let added_keys: Vec<PublicKey> = (0..NUM_ADDED_KEYS)
        .map(|i| {
            InMemorySigner::from_seed(account.clone(), KeyType::ED25519, &format!("added{i}"))
                .public_key()
        })
        .collect();
    let read_receipt =
        create_receipt_with_actions(account.clone(), signer.clone(), vec![read_action.clone()]);
    let read_and_add_keys_receipt = create_receipt_with_actions(
        account.clone(),
        signer,
        std::iter::once(read_action)
            .chain(added_keys.iter().map(|public_key| {
                Action::AddKey(Box::new(AddKeyAction {
                    public_key: public_key.clone(),
                    access_key: AccessKey::full_access(),
                }))
            }))
            .collect(),
    );

    // The read fills the receipt's allowance and still fits, so a real receipt could do it.
    let (_, read_status) = apply_receipt(&apply_state, root, &read_receipt);
    assert_matches!(read_status, ExecutionStatus::SuccessValue(_));

    // Before the feature version only the `FunctionCall` is bounded, so the keys
    // record past the limit unchecked.
    apply_state.current_protocol_version = feature_version - 1;
    let (_, status_before) = apply_receipt(&apply_state, root, &read_and_add_keys_receipt);
    assert_matches!(status_before, ExecutionStatus::SuccessValue(_));

    apply_state.current_protocol_version = PROTOCOL_VERSION;
    let (apply_result, status_after) =
        apply_receipt(&apply_state, root, &read_and_add_keys_receipt);
    let action_error = assert_matches!(
        status_after,
        ExecutionStatus::Failure(TxExecutionError::ActionError(action_error)) => action_error
    );
    assert_eq!(
        action_error.kind,
        ActionErrorKind::ReceiptStorageProofSizeExceeded { limit: limit as u64 }
    );
    let failed_index = action_error.index.unwrap();
    assert!(failed_index > 0, "receipt should fail on an `AddKey`, not on the read");

    let root = commit_apply_result(&apply_result, &mut apply_state, &tries, shard_uid);
    let state = tries.new_trie_update(shard_uid, root);
    for public_key in &added_keys {
        assert_eq!(get_access_key(&state, &account, public_key).unwrap(), None);
    }
}
```
