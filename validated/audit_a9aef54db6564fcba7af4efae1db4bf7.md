### Title
`RecordedStorageCounter::observe_size` is never invoked by the yield-ID host functions, letting a `FunctionCall` receipt grow its real storage proof past `per_receipt_storage_proof_size_limit` undetected - ([File: runtime/near-vm-runner/src/logic/logic.rs])

### Summary
`VMLogic::storage_write/storage_read/storage_remove/storage_has_key` are the only host functions that call `self.recorded_storage_counter.observe_size(self.ext.get_recorded_storage_size())` after touching the trie. The yield-ID host functions (`promise_yield_create_with_id` → `External::create_promise_yield_receipt_with_id`, and `promise_yield_resume_with_yield_id` → `External::submit_promise_resume_data_with_yield_id`) also perform genuine trie reads/writes (`has_yield_id_mapping`, `set_yield_id_mapping`, `get_data_id_for_yield_id`, `remove_yield_id_mappings`) but never call `observe_size`, so their contribution to the recorded storage proof is invisible to the in-VM `per_receipt_storage_proof_size_limit` enforcement.

### Finding Description
`RecordedStorageCounter` (`runtime/near-vm-runner/src/logic/recorded_storage_counter.rs:19-33`) is the only mechanism enforcing `per_receipt_storage_proof_size_limit` inside a running `FunctionCall`. It is seeded once per receipt at `Ctx::new` from `ext.storage_proof_size_before_receipt()` [1](#0-0) , and it is only actively checked wherever a host function explicitly calls `observe_size`. That call site exists exclusively in `storage_write`, `storage_read`, `storage_remove`, and `storage_has_key` [2](#0-1) [3](#0-2) [4](#0-3) [5](#0-4)  — a grep of the whole crate confirms only 4 call sites in each backend (`logic.rs` and `wasmtime_runner/logic.rs`).

`create_promise_yield_receipt_with_id`, reachable from the `promise_yield_create_with_id` host function, does a trie lookup (`has_yield_id_mapping`) and, if the key is new, two trie writes (`set_yield_id_mapping`, `set_promise_yield_status`) — all through `self.trie_update`, i.e. the same recording `TrieUpdate` that feeds the state witness proof: [6](#0-5) 

None of these calls are followed by `recorded_storage_counter.observe_size(...)`. The `External` trait doc explicitly documents that this mapping "is stored in the trie for duplicate detection" [7](#0-6)  and that resume "looks up the corresponding `data_id` from the trie mapping" [8](#0-7) , confirming these are genuine trie-proof-recording paths that were left out of the per-receipt observe_size enumeration.

Because a contract can choose an arbitrary 32-byte `user_yield_id` per call (`TrieKey::YieldIdToDataId { receiver_id, yield_id }` [9](#0-8) ), each distinct, previously-unseen `yield_id` forces a fresh root-to-leaf trie walk, recording new nodes into the proof each time (this happens because the key is essentially random/user-chosen and does not collide with previous lookups). Since PV86's only in-execution safety net is `RecordedStorageCounter`, and that counter is never advanced by this code path, the accumulated real proof size for the receipt can grow unbounded relative to the 4MB `per_receipt_storage_proof_size_limit`, as long as the contract never calls `storage_read/write/remove/has_key` in the same receipt (which would be the only other point where the accumulated size gets checked and could retroactively fail the receipt).

The receipt-level fallback check that could catch this after each action — `storage_proof_limit_for_all_actions` in `apply_action` — is gated by `ProtocolFeature::EnforceStorageProofLimitForAllActions`, which is a distinct, later feature explicitly documented as compensating for the fact that "the `RecordedStorageCounter` only runs inside the VM" [10](#0-9) ; the code comment at the call site further notes the same limitation [11](#0-10) . Whether that feature is active at the exact deployed PV86 mainnet configuration referenced by the question needs to be checked against the live `ProtocolFeature` activation table; if it is not yet active (as its ordering in the enum after `EnforcePerReceiptStorageProofLimit` suggests), there is no receipt-level backstop at all for this gap, and a single `FunctionCall` action's real recorded proof can exceed the intended 4MB hard limit purely via yield-ID host calls.

### Impact Explanation
This does not cause direct fund loss, but it undermines a documented consensus-safety limit (`per_receipt_storage_proof_size_limit`) whose entire purpose is to bound `ChunkStateWitness` size to a ~21MB budget [12](#0-11) . An attacker-controlled receipt that silently exceeds this bound produces an oversized witness component that is not rejected by the mechanism designed to reject it, risking degraded chunk distribution/validation (a liveness/availability concern for the shard) rather than state-root divergence, since chunk producers and validators execute the same deterministic trie walk and would still agree on the resulting state — the risk is witness-size blowup evading the intended hard cap, not consensus divergence.

### Likelihood Explanation
Exploitability requires only an unprivileged account: deploy a contract exposing a loop that calls `promise_yield_create_with_id` with distinct `yield_id`s and never calls `storage_read/write/remove/has_key` in the same receipt. Feasibility is bounded by ordinary gas costs (each trie touch is still gas-metered via `touching_trie_node`) and by `max_promises_per_function_call_action` (1024) for the successful (non-duplicate) branch, which caps how many new receipts a single call can create; the failing (duplicate) branch is cheaper but repeats the same already-recorded key so it does not add new proof bytes. The realistic worst case is bounded by 1024 fresh yield-ID trie walks per receipt, which — depending on trie depth/node sizes — plausibly reaches multi-megabyte proof sizes without ever triggering `RecordedStorageExceeded`. Repeatability is straightforward (same technique reusable every receipt); cost is just normal gas plus a fresh contract deployment.

### Recommendation
Add `self.recorded_storage_counter.observe_size(self.ext.get_recorded_storage_size())?` calls immediately after the trie-touching operations in `promise_yield_create_with_id` (post `create_promise_yield_receipt_with_id`) and `promise_yield_resume_with_yield_id` (post `submit_promise_resume_data_with_yield_id`) in both `runtime/near-vm-runner/src/logic/logic.rs` and `runtime/near-vm-runner/src/wasmtime_runner/logic.rs`, mirroring the pattern used in `storage_write`/`storage_read`/`storage_remove`/`storage_has_key`. More generally, audit every `External` method that can record trie proof (including any future ones) and enforce `observe_size` centrally (e.g., wrap `ext.get_recorded_storage_size()` calls or invoke `observe_size` in a single dispatch point after every `ext.*` call that can touch the trie) rather than relying on manual per-host-function calls.

### Proof of Concept
1. Unit test enumerating `External` trait methods that read/write `TrieKey` entries (`storage_set/get/remove/has_key`, `create_promise_yield_receipt_with_id`, `submit_promise_resume_data_with_yield_id`), and asserting via a wrapped/counting `External`+`RecordedStorageCounter` test harness that `observe_size` is invoked at least once per call for each — this test should fail today for the two yield-ID methods.
2. Runtime-level integration test (in `runtime/runtime/src/tests/apply.rs`, alongside `test_per_receipt_storage_proof_size_limit`): deploy a contract method that loops calling `promise_yield_create_with_id` with distinct random 32-byte yield IDs (no other storage host calls in the same receipt) enough times/with enough state pre-populated so total trie-proof size clearly exceeds `per_receipt_storage_proof_size_limit` (4MB); apply the receipt and assert that execution **succeeds** (`ExecutionStatus::SuccessValue`) while `apply_result.trie_changes`'s / `state_update.trie.recorded_storage_size_upper_bound()` diff for that receipt exceeds the configured limit — demonstrating the limit was silently bypassed instead of raising `ActionErrorKind::FunctionCallError(... RecordedStorageExceeded ...)`.

### Citations

**File:** runtime/near-vm-runner/src/wasmtime_runner/mod.rs (L345-350)
```rust
        let current_account_locked_balance = context.account_locked_balance;
        let config = Arc::clone(&result_state.config);
        let recorded_storage_counter = RecordedStorageCounter::new(
            ext.storage_proof_size_before_receipt(),
            result_state.config.limit_config.per_receipt_storage_proof_size_limit,
        );
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/logic.rs (L4747-4749)
```rust
    let evicted = ctx.ext.storage_set(&mut ctx.result_state.gas_counter, &key, &value)?;
    let storage_config = &ctx.fees_config.storage_usage_config;
    ctx.recorded_storage_counter.observe_size(ctx.ext.get_recorded_storage_size())?;
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/logic.rs (L4844-4844)
```rust
    ctx.recorded_storage_counter.observe_size(ctx.ext.get_recorded_storage_size())?;
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/logic.rs (L4909-4909)
```rust
    ctx.recorded_storage_counter.observe_size(ctx.ext.get_recorded_storage_size())?;
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/logic.rs (L4969-4969)
```rust
    ctx.recorded_storage_counter.observe_size(ctx.ext.get_recorded_storage_size())?;
```

**File:** runtime/runtime/src/ext.rs (L371-400)
```rust
    fn create_promise_yield_receipt_with_id(
        &mut self,
        receiver_id: AccountId,
        user_yield_id: YieldId,
    ) -> Result<Option<(ReceiptIndex, CryptoHash)>, VMLogicError> {
        // Check for duplicate yield_id in trie. TrieUpdate also reflects writes from earlier
        // calls within the same function call, so this also catches in-transaction duplicates.
        if has_yield_id_mapping(self.trie_update, &receiver_id, user_yield_id)
            .map_err(wrap_storage_error)?
        {
            return Ok(None);
        }

        let input_data_id = self.generate_data_id();

        // Store bidirectional yield_id <-> data_id mappings
        set_yield_id_mapping(&mut self.trie_update, &receiver_id, user_yield_id, input_data_id);

        let receipt_index =
            self.receipt_manager.create_promise_yield_receipt(input_data_id, receiver_id.clone());

        set_promise_yield_status(
            &mut self.trie_update,
            &receiver_id,
            input_data_id,
            PromiseYieldStatus::Yielded,
        );

        Ok(Some((receipt_index, input_data_id)))
    }
```

**File:** runtime/near-vm-runner/src/logic/dependencies.rs (L306-320)
```rust
    /// Create a PromiseYield action receipt with a user-provided yield ID.
    ///
    /// Returns `Some((ReceiptIndex, data_id))` of the newly created receipt on success, or
    /// `None` if a yield with the same `user_yield_id` is already pending for this account.
    /// The yield_id -> data_id mapping is stored in the trie for duplicate detection.
    ///
    /// # Arguments
    ///
    /// * `receiver_id` - account id of the receiver of the receipt created
    /// * `user_yield_id` - user-provided 32-byte yield identifier
    fn create_promise_yield_receipt_with_id(
        &mut self,
        receiver_id: AccountId,
        user_yield_id: YieldId,
    ) -> Result<Option<(ReceiptIndex, CryptoHash)>, VMLogicError>;
```

**File:** runtime/near-vm-runner/src/logic/dependencies.rs (L341-352)
```rust
    /// Resume a yield previously created by `promise_yield_create_with_id`, using the user-provided
    /// `yield_id` instead of the runtime-generated `data_id`.
    ///
    /// The runtime looks up the corresponding `data_id` from the trie mapping and submits the
    /// resume data. Returns `Ok(true)` if the yield was found and resume was submitted,
    /// `Ok(false)` if no yield exists for the given `yield_id`.
    ///
    /// # Arguments
    ///
    /// * `user_yield_id` - user-provided 32-byte yield identifier from `yield_create_with_id`
    /// * `data` - contents of the DataReceipt
    fn submit_promise_resume_data_with_yield_id(
```

**File:** core/primitives/src/trie_key.rs (L282-287)
```rust
    /// Mapping from user-provided yield ID to runtime-generated data ID.
    /// Used by `promise_yield_create_with_id` for duplicate detection.
    YieldIdToDataId {
        receiver_id: AccountId,
        yield_id: YieldId,
    } = col::YIELD_ID_TO_DATA_ID,
```

**File:** core/primitives-core/src/version.rs (L451-457)
```rust
    /// Extend the per-receipt storage proof limit to every action kind. The
    /// `RecordedStorageCounter` only runs inside the VM, so it bounds
    /// `FunctionCall` actions alone; other actions in the same receipt could
    /// record proof past the limit. Check the receipt's recorded size after
    /// each action and fail the receipt with
    /// `ActionErrorKind::ReceiptStorageProofSizeExceeded` once it goes over.
    EnforceStorageProofLimitForAllActions,
```

**File:** runtime/runtime/src/lib.rs (L879-889)
```rust
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
```

**File:** docs/misc/state_witness_size_limits.md (L1-31)
```markdown
## State witness size limits

Some limits were introduced to keep the size of `ChunkStateWitness` reasonable.
`ChunkStateWitness` contains all the incoming transactions and receipts that will be processed during chunk application and in theory a single receipt could be tens of megabytes in size. Distributing a `ChunkStateWitness` this large would be troublesome, so we limit the size and number of transactions, receipts, etc. The limits aim to keep the total uncompressed size of `ChunkStateWitness` under 21MiB.

There are two types of size limits:

* Hard limit - the size must be below this limit, anything else is considered invalid
* Soft limit - things are added until the limit is exceeded, after that things stop being added. The last added thing is allowed to slightly exceed the limit.

The limits are:

* `max_transaction_size = 1.5 MiB`
  * All transactions must be below 1.5 MiB, otherwise they'll be considered invalid and rejected.
  * Previously was 4MiB, now reduced to 1.5MiB
* `max_receipt_size - 4 MiB`:
  * All receipts must be below 4 MiB, otherwise they'll be considered invalid and rejected.
  * Previously there was no limit on receipt size. Set to 4MiB, might be reduced to 1.5MiB in the future to match the transaction limit.
* `max_receipt_total_input_size - 4 MiB + 640 B`
  * Hard limit on the combined size of a receipt's resolved promise inputs (the `ReceivedData` referenced by its `input_data_ids`). Receipts which exceed it fail with `TotalPromiseInputSizeExceeded` without executing their actions.
  * These inputs are read before `per_receipt_storage_proof_size_limit` starts counting, so without this limit a single receipt could pull `max_number_input_data_dependencies * max_receipt_size` (128 * 4 MiB) into the witness.
  * The limit is `max_length_returned_data` (4 MiB) plus the worst-case per-input framing overhead (128 * 5 bytes), so 4 MiB of input data always fits no matter how it's split across data receipts.
* `combined_transactions_size_limit - 4 MiB`
  * Hard limit on total size of transactions from this and previous chunk. `ChunkStateWitness` contains transactions from two chunks, this limit applies to the sum of their sizes.
* `new_transactions_validation_state_size_soft_limit - 500 KiB`
  * Validating new transactions generates storage proof (recorded trie nodes), which has to be limited. Once transaction validation generates more storage proof than this limit, the chunk producer stops adding new transactions to the chunk.
* `per_receipt_storage_proof_size_limit - 4 MB`
  * Executing a receipt generates storage proof. A single receipt is allowed to generate at most 4MB of storage proof. This is a hard limit, receipts which generate more than that will fail.
* `main_storage_proof_size_soft_limit - 4 MB`
  * This is a limit on the total size of storage proof generated by receipts in one chunk. Once receipts generate more storage proof than this limit, the chunk producer stops processing receipts and moves the rest to the delayed queue.
  * It's a soft limit, which means that the total size of storage proof could reach 8 MB (3.99MB + one receipt which generates 4MB of storage proof)
```
