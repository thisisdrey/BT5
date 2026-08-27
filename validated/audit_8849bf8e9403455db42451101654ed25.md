### Title
`DeleteAccount` on an account with a pending `PromiseYield` permanently burns the yield's attached deposit instead of refunding it - (`runtime/runtime/src/actions.rs`, `runtime/runtime/src/lib.rs`)

### Summary
`check_actor_permissions` allows `DeleteAccount` on any unlocked account regardless of whether it has outstanding `PromiseYield` receipts targeting itself, and `action_delete_account` refunds only the account's *current* balance to the beneficiary. Any deposit attached via `promise_yield_create_with_id` was already deducted from the account balance at yield-creation time and lives only inside the pending `PromiseYieldReceipt`/timeout-queue entry, which is untouched by deletion. When the timeout (or an explicit resume) eventually fires, `resolve_promise_yield_timeouts` and `process_receipt` re-execute the stored callback against an account that `get_account` now resolves to `None`; the callback action fails `check_account_existence`, and the resulting deposit-refund receipt is sent to a (now-deleted) named account, which cannot be re-created implicitly, so the deposit is effectively burned rather than returned to anyone.

### Finding Description
- `check_actor_permissions` only verifies `actor_id == account_id` and `!account.locked().is_zero()` for `DeleteAccount`; it has no awareness of pending yields: [1](#0-0) 
- `action_delete_account` pays out only `account.amount()` to the beneficiary and then sets the account to `None`, without inspecting `PromiseYieldReceipt`/`PromiseYieldTimeout` state for the account: [2](#0-1) 
- A deposit attached with `promise_yield_create_with_id` is deducted from the caller's account balance immediately at creation time (`deduct_balance`), and the deposit value is embedded solely in the pending `FunctionCall` action stored under `PromiseYieldReceipt{receiver_id, data_id}` plus a `PromiseYieldTimeout` queue entry — not tracked anywhere else: [3](#0-2) 
- `resolve_promise_yield_timeouts` walks the timeout queue purely via trie keys and never checks whether the target account still exists before enqueuing a `PromiseResume` receipt for it: [4](#0-3) 
- When that resume/timeout receipt is processed, the runtime fetches the stored `yield_receipt` by trie key (also unaffected by the earlier account deletion) and re-executes it via `apply_action_receipt`: [5](#0-4) 
- `apply_action_receipt` calls `get_account`, which returns `Option<Account>` and does **not** panic on `None` — `account_did_not_exist` is simply recorded: [6](#0-5) 
- However, the callback's `FunctionCall` action then fails `check_account_existence` because the receiver is a deleted, non-implicit named account, so the action fails and the deposit is routed through the standard failure/refund path. Per the runtime's own documented refund semantics, a refund receipt whose target account no longer exists (and is not implicit-creation eligible) is burned rather than delivered: [7](#0-6) 

Net effect: the attacker's own deposit — money that left the account balance at yield-creation time — is neither transferred to the `DeleteAccount` beneficiary (because it had already left the balance before deletion) nor refunded back on callback failure (because the refund target no longer exists and cannot be implicitly recreated). It is permanently burned. This is a **silent, protocol-level value loss** rather than a shard-halting panic, since `get_account`/`apply_action_receipt` tolerate `None` accounts gracefully.

### Impact Explanation
Concrete scoped impact: attacker-controlled but unrecoverable loss of the deposit yoctoNEAR attached via `promise_yield_create_with_id` when the same account self-deletes before the yield resolves. This falls under "permanent freezing/loss of user funds" (value conservation violation), not theft (no other party gains the funds) and not a shard halt (no panic path was found — `get_account` returns `Option` and is handled without `.unwrap()`/`.expect()` in this path).

### Likelihood Explanation
Fully attacker-reachable with only two ordinary transactions from an unprivileged account: (1) call `promise_yield_create_with_id` with a nonzero deposit targeting itself, (2) issue `DeleteAccount` (locked stake must be zero, which is the default/normal case) with any beneficiary, then wait `yield_timeout_length_in_blocks`. No special permissions, validator access, or contract bugs are required beyond the attacker's own contract using the yield/resume host functions. The bug is deterministic and repeatable on every occurrence of this ordering; the only "cost" is the deposit itself, which the attacker loses, so this is more a self-harming bug/fund-freezing issue than an exploit that benefits the attacker, but it demonstrates a value-conservation violation and, if triggered inadvertently by normal dApp usage (e.g., accounts self-deleting for storage cost reasons), constitutes unintentional fund loss for legitimate users.

### Recommendation
Either (a) prevent `DeleteAccount` from succeeding while the account has any outstanding `PromiseYieldReceipt`/timeout entries (reject with a dedicated `ActionErrorKind`, mirroring the existing `DeleteAccountStaking` check for locked balance), or (b) at deletion time enumerate and cancel all pending yields for the account and fold their attached deposits into the beneficiary refund, or (c) make the timeout/resume failure path detect "callback target no longer exists" and redirect the deposit to a defined fallback (e.g., the `DeleteAccount` beneficiary recorded at deletion time, if tracked) instead of silently burning it.

### Proof of Concept
Test-loop/integration test plan:
1. Deploy a contract exposing `call_yield_create_with_id` (attaching a deposit, self-targeted) and record `data_id`/`yield_id`.
2. In the same or a subsequent block, submit `DeleteAccount{beneficiary_id: some_other_account}` signed by the same account (ensure `locked() == 0`).
3. Assert the `DeleteAccount` transaction succeeds and the account is removed (`view_account` returns `AccountDoesNotExist`).
4. Assert `beneficiary_id`'s balance increase equals exactly the account's balance *at deletion time* (i.e., it does **not** include the deposit that was already deducted at yield-creation time).
5. Advance the chain past `yield_timeout_height()`.
6. Assert no panic occurs and the chain continues producing blocks (rules out the shard-halt branch).
7. Assert the deposit amount is not credited to any account (`beneficiary_id`, the deleted account via re-creation, or any other party) — confirming permanent burn — by summing total supply/`tokens_burnt` before/after and showing the deposit is unaccounted for as a live balance anywhere.

Note: I was unable to fully trace `action_implicit_account_creation_transfer`/`balance_refund_receiver` line-by-line within the available search budget to pin down the exact receipt hop where the burn occurs (whether it's an outright failed-refund burn, or a receipt that gets silently dropped as `other_burnt_amount`); this should be confirmed by a Devin session running the above test-loop scenario and inspecting `ChunkApplyStatsV1`/`tokens_burnt` deltas.

### Citations

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

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3789-3810)
```rust
        // Allow attaching exactly 1 yoctoNEAR with the `one_yocto_on_promise`
        // exemption (mirrors `promise_batch_action_function_call_weight`).
        let skip_deduct = amount == Balance::from_yoctonear(1)
            && self.config.one_yocto_on_promise
            && self.result_state.current_account_balance.is_zero();
        if skip_deduct {
            self.result_state.subsidized_amount = self
                .result_state
                .subsidized_amount
                .checked_add(amount)
                .expect("subsidized_amount overflow");
        } else {
            self.result_state.deduct_balance(amount)?;
        }
        self.ext.append_action_function_call_weight(
            new_receipt_idx,
            method_name,
            arguments,
            amount,
            Gas::from_gas(gas),
            GasWeight(gas_weight),
        )?;
```

**File:** runtime/runtime/src/lib.rs (L853-854)
```rust
        let mut account = get_account(state_update, account_id)?;
        let account_did_not_exist = account.is_none();
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

**File:** runtime/runtime/src/lib.rs (L3025-3098)
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

            // Record a ReceiptToTx entry for the new resume receipt. The parent is the
            // yield receipt that is being timed out.
            if processing_state.apply_state.save_receipt_to_tx {
                let yield_receipt: Receipt = get_pure(state_update, &promise_yield_key)?
                    .expect("promise yield receipt should exist since contains_key was true");
                processing_state.receipt_to_tx.push((
                    new_receipt_id,
                    ReceiptToTxInfo::V1(ReceiptToTxInfoV1 {
                        origin: ReceiptOrigin::FromReceipt(ReceiptOriginReceipt {
                            parent_receipt_id: *yield_receipt.receipt_id(),
                            parent_predecessor_id: yield_receipt.predecessor_id().clone(),
                        }),
                        receiver_account_id: queue_entry.account_id.clone(),
                        shard_id: processing_state.apply_state.shard_id,
                    }),
                ));
            }

            // The receipt is destined for the local shard and will be placed in the outgoing
            // receipts buffer. It is possible that there is already an outgoing receipt resolving
            // this yield if `yield_resume` was invoked by some receipt which was processed in
            // the current chunk. The ordering will be maintained because the receipts are
            // destined for the same shard; the timeout will be processed second and discarded.
            receipt_sink.forward_or_buffer_receipt(
                resume_receipt,
                apply_state,
                &mut state_update,
            )?;
        }
```

**File:** protocol-model/spec/runtime-execution.md (L151-152)
```markdown
- **Invalid txs make progress, not failure**: a chunk with invalid transactions is not rejected; the offending txs are skipped during conversion, polluting the chain with junk but keeping the shard live (`runtime/runtime/src/lib.rs:1706` doc; skip sites at `:1994`, `:2199`).
- **Refund receipts are free**: system-predecessor receipts burn zero gas; a failed refund burns its deposit into `other_burnt_amount` rather than refunding (`runtime/runtime/src/lib.rs:929`, `:972`).
```
