Confirmed: `EnforcePerReceiptStorageProofLimit` activates at PV 86, while `EnforceStorageProofLimitForAllActions` activates one version later at PV 87 [1](#0-0) . This creates exactly the gap described in the question, but only for a single PV (86).

### Title
Non-FunctionCall actions (AddKey/DeleteKey) bypass all recorded-storage-proof metering at PV 86 - ([File: runtime/runtime/src/lib.rs])

### Summary
At protocol version 86, `EnforcePerReceiptStorageProofLimit` is active but `EnforceStorageProofLimitForAllActions` is not yet active, so the per-action check at `apply_action` (line 928-944) never fires because `storage_proof_limit_for_all_actions` is `None` [2](#0-1) . Since `RecordedStorageCounter` in the VM only guards `FunctionCall` actions via `storage_write`/`storage_read`, a receipt consisting solely of `AddKey`/`DeleteKey` actions against an account with a large access-key trie subtree can accumulate unbounded recorded storage proof at PV 86, with no metering check anywhere in the path.

### Finding Description
The runtime computes `storage_proof_size_before_receipt` gated only by `EnforcePerReceiptStorageProofLimit` (PV≥86), and separately computes `storage_proof_limit_for_all_actions` gated by `EnforceStorageProofLimitForAllActions` (PV≥87) [3](#0-2) . The actual enforcement branch at line 928 requires **both** to be `Some` simultaneously: `if let (true, Some(size_before), Some(limit)) = (...)` [4](#0-3) . Because `EnforceStorageProofLimitForAllActions` only turns on at PV 87 [1](#0-0) , at PV 86 `limit` is always `None`, so this branch never executes for **any** action type, including `FunctionCall`. For `FunctionCall` actions, the in-VM `RecordedStorageCounter::observe_size` still provides a backstop (seeded via the VM's own limit check), so `FunctionCall` receipts remain bounded. But `AddKey`/`DeleteKey` and other non-`FunctionCall` actions never invoke `recorded_storage_counter` at all — that counter only exists inside `VMLogic`, constructed per-`FunctionCall` invocation. `check_actor_permissions`/access-key lookups for `AddKey`/`DeleteKey` go through the trie directly without any proof-size ceiling at PV 86.

### Impact Explanation
This is a genuine metering gap within the single-version window PV 86: a receipt of pure `AddKey`/`DeleteKey` actions against an account with a maximally deep access-key trie subtree can inflate the recorded storage witness/proof unboundedly, contributing to chunk witness blow-up and increased block/witness-generation time, matching the category of chunk-witness-size/resource-exhaustion issues. It does not on its own cause fund theft, double-spend, or state-root divergence — all validators would observe the same (large) proof deterministically, so it is a liveness/performance degradation risk, not a consensus-safety break.

### Likelihood Explanation
The window is extremely narrow: it only applies while a live network is running exactly at protocol version 86, before it advances to 87 (the two features were stabilized one version apart intentionally as a staged rollout). On any deployed network already at PV≥87 (which stabilized these features together) this gap does not exist. An attacker also needs to first build a deep access-key subtree by submitting many `AddKey` transactions (paying storage-staking deposit for each key), so there is real cost. Given the narrow, transient PV window, the practical likelihood on a real running mainnet/testnet is very low, but the gap is real and reachable by an unprivileged account within that window.

### Recommendation
Decouple the per-action storage-proof enforcement from `EnforceStorageProofLimitForAllActions`: at PV 86 (once `EnforcePerReceiptStorageProofLimit` is active), the runtime should still enforce `per_receipt_storage_proof_size_limit` for all actions, not only conditionally when the later feature is also active. Concretely, make `storage_proof_limit_for_all_actions` depend solely on `EnforcePerReceiptStorageProofLimit` (with `EnforceStorageProofLimitForAllActions` perhaps controlling only a different aspect, e.g. whether the limit is stricter/enabled for FunctionCall specifically), so the two features aren't required to both be active for basic non-FunctionCall metering to apply.

### Proof of Concept
Integration test in `runtime/runtime/src/tests/apply.rs`:
1. Set `current_protocol_version` to exactly 86 (`EnforcePerReceiptStorageProofLimit` on, `EnforceStorageProofLimitForAllActions` off).
2. Fund an account and submit enough `AddKey` transactions/receipts to build a deep access-key trie subtree.
3. Submit a single receipt composed only of `AddKey`/`DeleteKey` actions that forces large trie reads (e.g., repeated `DeleteKey` lookups against the deep subtree).
4. Assert that `state_update.trie.recorded_storage_size_upper_bound()` grows past `per_receipt_storage_proof_size_limit` without the receipt failing with `ActionErrorKind::ReceiptStorageProofSizeExceeded`, demonstrating the metering gap.
5. Repeat with PV=87 and confirm the same receipt now fails with `ReceiptStorageProofSizeExceeded`, confirming the fix boundary [5](#0-4) .

### Citations

**File:** core/primitives-core/src/version.rs (L598-604)
```rust
            ProtocolFeature::EnforcePerReceiptStorageProofLimit => 86,

            ProtocolFeature::FixContractLoadingError => 86,
            ProtocolFeature::RejectEmptyMethodName => 87,
            ProtocolFeature::RemoveGasRewards => 87,
            ProtocolFeature::EnforceStorageProofLimitForAllActions => 87,
            ProtocolFeature::ReceiptPromiseInputSizeLimit => 87,
```

**File:** runtime/runtime/src/lib.rs (L871-945)
```rust
            let storage_proof_size_before_receipt =
                if ProtocolFeature::EnforcePerReceiptStorageProofLimit
                    .enabled(apply_state.current_protocol_version)
                {
                    Some(state_update.trie.recorded_storage_size_upper_bound())
                } else {
                    None
                };
            // The in-VM `RecordedStorageCounter` only bounds `FunctionCall` actions.
            let storage_proof_limit_for_all_actions =
                ProtocolFeature::EnforceStorageProofLimitForAllActions
                    .enabled(apply_state.current_protocol_version)
                    .then(|| {
                        apply_state
                            .config
                            .wasm_config
                            .limit_config
                            .per_receipt_storage_proof_size_limit
                    });

            // Executing actions one by one
            for (action_index, action) in action_receipt.actions().iter().enumerate() {
                let action_hash = create_action_hash_from_receipt_id(
                    receipt.receipt_id(),
                    apply_state.block_height,
                    action_index,
                );
                let mut new_result = self.apply_action(
                    action,
                    state_update,
                    apply_state,
                    preparation_pipeline,
                    &mut account,
                    &mut actor_id,
                    receipt,
                    &action_receipt,
                    Arc::clone(&promise_results),
                    &action_hash,
                    action_index,
                    &action_receipt.actions(),
                    epoch_info_provider,
                    storage_proof_size_before_receipt,
                )?;
                if new_result.result.is_ok() {
                    if let Err(e) = new_result.new_receipts.iter().try_for_each(|receipt| {
                        validate_receipt(
                            &apply_state.config.wasm_config.limit_config,
                            receipt,
                            apply_state.current_protocol_version,
                            ValidateReceiptMode::NewReceipt,
                        )
                    }) {
                        new_result.result =
                            Err(ActionErrorKind::NewReceiptValidationError(e).into());
                    }
                }
                result.merge(new_result)?;
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

**File:** runtime/runtime/src/tests/apply.rs (L1-1)
```rust
use super::GAS_PRICE;
```
