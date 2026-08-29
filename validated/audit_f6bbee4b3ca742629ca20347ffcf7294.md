This confirms the premise is invalid. `process_receipts` runs strictly sequentially over a single shared `processing_state.state_update` (a `TrieUpdate`): `process_local_receipts` iterates local receipts one at a time in a `for` loop calling `process_receipt_and_instant_receipts` synchronously for each [1](#0-0) , then `process_delayed_receipts` drains the delayed queue afterward, also one receipt at a time against the same `state_update` [2](#0-1) , and only then `process_incoming_receipts` runs [3](#0-2) . There is no concurrency and no "batching of reads" across receipts — each receipt's `apply_action`/`apply_delegate_action`/`validate_delegate_action_key` call reads and writes the live `TrieUpdate` before the next receipt in the loop begins, and `TrieUpdate` mutations are visible to subsequent reads within the same update (commit happens per receipt via `state_update.commit`/`rollback` as described in the receipt execution flow) [4](#0-3) .

The "prefetch" mechanism the question alludes to (`runtime/runtime/src/prefetch.rs`) only warms the trie node cache by issuing read-ahead requests for `TrieKey::gas_key_nonce`; it does not supply a stale/batched value to `validate_delegate_action_key`, which always calls `get_gas_key_nonce(state_update, ...)` fresh against the live `TrieUpdate` [5](#0-4) [6](#0-5) . Prefetching is explicitly documented as "best-effort" and separate from the actual state-update reads used for validation [7](#0-6) .

So for the attack to work, both `Delegate` receipts carrying nonce `N` at the same `nonce_index` would need their `validate_delegate_action_key` calls to execute concurrently or out of commit-order on the same `TrieUpdate`. That never happens: whichever receipt (local or delayed) is processed first in the fixed local → delayed → incoming order will call `set_gas_key_nonce(state_update, ..., delegate_nonce.nonce())` and commit before the loop advances to the next receipt [8](#0-7) . The second receipt's `get_gas_key_nonce` read will therefore see the updated nonce and its `delegate_nonce.nonce() <= current_nonce` check will fail, producing `DelegateActionInvalidNonce` [9](#0-8) . This is exactly what the existing test `test_gas_key_delegate_v2_meta_transaction` demonstrates: replaying the identical `SignedDelegateAction` is rejected [10](#0-9) .

#No vulnerability found for this question.

### Citations

**File:** runtime/runtime/src/lib.rs (L713-730)
```rust
            Action::DeleteAccount(delete_account) => {
                metrics::ACTION_CALLED_COUNT.delete_account.inc();
                action_delete_account(
                    state_update,
                    account,
                    actor_id,
                    receipt,
                    &mut result,
                    account_id,
                    delete_account,
                    &apply_state.config,
                    apply_state.current_protocol_version,
                )?;
            }
            Action::Delegate(signed_delegate_action) => {
                metrics::ACTION_CALLED_COUNT.delegate.inc();
                apply_delegate_action(
                    state_update,
```

**File:** runtime/runtime/src/lib.rs (L1810-1813)
```rust
        if let Some(prefetcher) = &mut processing_state.prefetcher {
            // Prefetcher is allowed to fail
            _ = prefetcher.prefetch_transactions_data(&signed_txs);
        }
```

**File:** runtime/runtime/src/lib.rs (L2407-2442)
```rust
        for receipt in &local_receipts {
            if processing_state.total.compute >= compute_limit
                || processing_state.state_update.trie.check_proof_size_limit_exceed()
            {
                processing_state.delayed_receipts.push(
                    &mut processing_state.state_update,
                    &receipt,
                    &processing_state.apply_state,
                )?;
            } else {
                if let Some(nsi) = &mut next_schedule_after {
                    *nsi = nsi.saturating_sub(1);
                    if *nsi == 0 {
                        // We're about to process a receipt that has been submitted for
                        // preparation, so lets submit the next one in anticipation that it might
                        // be processed too (it might also be not if we run out of gas/compute.)
                        next_schedule_after = schedule_contract_preparation(
                            &mut processing_state.pipeline_manager,
                            &processing_state.state_update,
                            &mut prep_lookahead_iter,
                        );
                    }
                }
                // NOTE: We don't need to validate the local receipt, because it's just validated in
                // the `verify_and_charge_transaction`.
                self.process_receipt_and_instant_receipts(
                    &receipt,
                    &mut processing_state,
                    receipt_sink,
                    validator_proposals,
                )?;
                processing_state.processed_receipts.push(ProcessedReceipt {
                    receipt: receipt.clone(),
                    source: ReceiptSource::Local,
                });
            }
```

**File:** runtime/runtime/src/lib.rs (L2485-2545)
```rust
        loop {
            if processing_state.total.compute >= compute_limit
                || processing_state.state_update.trie.check_proof_size_limit_exceed()
            {
                break;
            }

            let receipt = if let Some(receipt) = processing_state
                .delayed_receipts
                .pop(&mut processing_state.state_update, &processing_state.apply_state.config)?
            {
                receipt.into_receipt()
            } else {
                // Break loop if there are no more receipts to be processed.
                break;
            };

            // TODO(resharding): Add metric for tracking number of
            delayed_receipt_count += 1;
            if let Some(nsi) = &mut next_schedule_after {
                *nsi = nsi.saturating_sub(1);
                if *nsi == 0 {
                    let mut prep_lookahead_iter =
                        processing_state.delayed_receipts.peek_iter(&processing_state.state_update);
                    next_schedule_after = schedule_contract_preparation(
                        &mut processing_state.pipeline_manager,
                        &processing_state.state_update,
                        &mut prep_lookahead_iter,
                    );
                }
            }

            if let Some(prefetcher) = &mut processing_state.prefetcher {
                // Prefetcher is allowed to fail
                _ = prefetcher.prefetch_receipts_data(std::slice::from_ref(&receipt));
            }

            // Validating the delayed receipt. If it fails, it's likely the state is inconsistent.
            validate_receipt(
                &processing_state.apply_state.config.wasm_config.limit_config,
                &receipt,
                protocol_version,
                ValidateReceiptMode::ExistingReceipt,
            )
            .map_err(|e| {
                StorageError::StorageInconsistentState(format!(
                    "Delayed receipt {:?} in the state is invalid: {}",
                    receipt, e
                ))
            })?;

            self.process_receipt_and_instant_receipts(
                &receipt,
                &mut processing_state,
                receipt_sink,
                validator_proposals,
            )?;
            processing_state
                .processed_receipts
                .push(ProcessedReceipt { receipt, source: ReceiptSource::Delayed });
        }
```

**File:** runtime/runtime/src/lib.rs (L2681-2715)
```rust
    fn process_receipts(
        &self,
        processing_state: &mut ApplyProcessingReceiptState,
        receipt_sink: &mut ReceiptSink,
    ) -> Result<ProcessReceiptsResult, RuntimeError> {
        let mut validator_proposals = vec![];
        let apply_state = &processing_state.apply_state;

        // TODO(#8859): Introduce a dedicated `compute_limit` for the chunk.
        // For now compute limit always matches the gas limit.
        let compute_limit = apply_state.gas_limit.map(|g| g.as_gas()).unwrap_or(u64::MAX);

        // We first process local receipts. They contain staking, local contract calls, etc.
        self.process_local_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;

        // Then we process the delayed receipts. It's a backlog of receipts from the past blocks.
        self.process_delayed_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;

        // And then we process the new incoming receipts. These are receipts from other shards.
        self.process_incoming_receipts(
            processing_state,
            receipt_sink,
            compute_limit,
            &mut validator_proposals,
        )?;
```

**File:** runtime/runtime/src/prefetch.rs (L130-142)
```rust
                            // A gas key delegate action also reads the per-index
                            // nonce row during validation; prefetch it too.
                            match delegate_action.nonce() {
                                TransactionNonce::GasKeyNonce { nonce_index, .. } => {
                                    let trie_key = TrieKey::gas_key_nonce(
                                        delegate_action.sender_id().clone(),
                                        delegate_action.public_key(),
                                        nonce_index,
                                    );
                                    self.prefetch_trie_key(trie_key)?;
                                }
                                TransactionNonce::Nonce { .. } => {}
                            }
```

**File:** runtime/runtime/src/actions.rs (L619-628)
```rust
            let current_nonce =
                get_gas_key_nonce(state_update, sender_id, public_key, nonce_index)?.ok_or_else(
                    || {
                        StorageError::StorageInconsistentState(format!(
                            "gas key nonce row missing for {} {} at in-range index {nonce_index} (num_nonces {})",
                            sender_id, public_key, gas_key_info.num_nonces,
                        ))
                    },
                )?;
            (current_nonce, DelegateNonceUpdate::GasKey { nonce_index })
```

**File:** runtime/runtime/src/actions.rs (L632-639)
```rust
    if delegate_nonce.nonce() <= current_nonce {
        result.result = Err(ActionErrorKind::DelegateActionInvalidNonce {
            delegate_nonce: delegate_nonce.nonce(),
            ak_nonce: current_nonce,
        }
        .into());
        return Ok(());
    }
```

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

**File:** test-loop-tests/src/tests/gas_keys.rs (L274-284)
```rust
    // Replaying the same delegate (same gas key nonce) is rejected.
    let block_hash = get_shared_block_hash(&env.node_datas, &env.test_loop.data);
    let replay_tx = SignedTransaction::from_actions(
        next_relayer_nonce(),
        relayer.clone(),
        sender.clone(),
        &relayer_signer,
        vec![Action::DelegateV2(Box::new(signed_delegate))],
        block_hash,
    );
    let replay_outcome = env.rpc_runner().execute_tx(replay_tx, Duration::seconds(5)).unwrap();
```
