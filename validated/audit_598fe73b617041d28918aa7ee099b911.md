### Title
Outgoing receipts can exceed `max_receipt_size` after post-validation mutation of `output_data_receivers` / return-value payloads - ([File: runtime/runtime/src/verifier.rs], [File: runtime/runtime/src/lib.rs])

### Summary
`validate_receipt` in `ValidateReceiptMode::NewReceipt` measures a receipt's borsh-serialized size via `borsh::object_length(receipt)` at the moment the receipt is constructed, but downstream promise-chaining logic (`promise_return`) and value-return handling can append `output_data_receivers` or wrap large return values into a `Data` receipt *after* this check, allowing the final serialized receipt included in a chunk/witness to exceed `max_receipt_size`. This is a real, already-tracked defect in the codebase (nearcore issue #12606), not a hypothetical.

### Finding Description
`validate_receipt` computes the size check only against the receipt as constructed at that instant: [1](#0-0) 

The code comment explicitly documents that `ValidateReceiptMode::ExistingReceipt` exists specifically because "there is a bug which allows to create receipts that are above the size limit... Runtime has to handle them gracefully until the receipt size limit bug is fixed," citing near/nearcore#12606: [2](#0-1) 

The repository's own integration tests (`test-loop-tests/src/tests/max_receipt_size.rs`) reproduce exactly this class of bug via two mechanisms:
1. `test_max_receipt_size_promise_return`: a promise-chain DAG where a receipt is validated at `max_receipt_size` and then `output_data_receivers` is mutated afterward, pushing it over the limit — the test comment states this "should be rejected, but currently isn't because of a bug." [3](#0-2) 
2. `test_max_receipt_size_value_return`: a large returned value is wrapped into a `Data` receipt that ends up larger than `max_receipt_size`, again validated as-created but not re-checked once wrapped. [4](#0-3) 

Both tests confirm the oversized receipt is not caught at creation-time and instead is asserted to appear later in the chain/witness via `assert_oversized_receipt_occurred`, which scans committed blocks for receipts above `max_receipt_size`: [5](#0-4) 

The docs on witness size limits describe `max_receipt_size` as a hard limit that "must be below this limit, otherwise it's considered invalid," underscoring that this is treated as a genuine protocol invariant that can be violated: [6](#0-5) 

The exact mechanism proposed in the question (summing `FunctionCall` args across many `promise_batch_action_function_call` calls, then a later append of return-value/log metadata pushing it over the limit) is a variant of the same root cause — `validate_receipt(NewReceipt)` snapshotting size at one point in time while later runtime logic (promise chaining / value wrapping) mutates the receipt without re-validating it. The codebase already demonstrates this class of defect concretely via the value-return and promise-return paths, though I could not fully trace whether the precise multi-`FunctionCall`-batch summation variant described in the question (as opposed to promise-chain `output_data_receivers` mutation or value-wrapping) is independently reachable — the existing tests cover the promise-return and value-return vectors, not a pure batch-of-`FunctionCall`-actions vector.

### Impact Explanation
This falls under "shard-halting/consensus divergence" — a receipt that exceeds `max_receipt_size` in the actual chunk witness violates a declared hard limit of the state-witness size-limiting scheme, whose stated purpose is to bound `ChunkStateWitness` size for validators (`docs/misc/state_witness_size_limits.md`). The runtime is explicitly documented to tolerate this "gracefully" via `ValidateReceiptMode::ExistingReceipt` rather than panicking, which mitigates an immediate crash, but the size-limit invariant that other validators/tooling depend on for witness bounding is broken. An unprivileged attacker (any funded account deploying and calling a contract) can trigger this via ordinary transactions.

### Likelihood Explanation
This requires only a funded account, a deployed wasm contract, and standard `promise_batch_create`/`promise_return`/value-return API usage — no elevated privileges. The existing repo tests (`test_max_receipt_size_promise_return`, `test_max_receipt_size_value_return`) already demonstrate concrete, reproducible triggers using the real `near_test_contracts::rs_contract()` test contract, confirming feasibility and repeatability at essentially default gas costs.

### Recommendation
Re-run `validate_receipt(..., ValidateReceiptMode::NewReceipt)` (or an equivalent size check) after all post-processing that can grow a receipt's serialized size — specifically after `output_data_receivers` are appended during promise-chain resolution (`promise_return`) and after return-value/log data is wrapped into outgoing `Data` receipts — rather than only validating the receipt as constructed prior to those mutations. This is tracked as near/nearcore#12606 and should be fixed at the root rather than only tolerated via `ValidateReceiptMode::ExistingReceipt`.

### Proof of Concept
Use the existing tests as the reproducible PoC baseline:
- `test-loop-tests/src/tests/max_receipt_size.rs::test_max_receipt_size_promise_return` — constructs a promise DAG `A -then-> B` where `A` creates receipt `C` at exactly `max_receipt_size` via `max_receipt_size_promise_return_method1`, then `output_data_receivers` is appended, pushing `C` over the limit; assert via `assert_oversized_receipt_occurred` that an oversized receipt appears in a produced block/witness despite passing creation-time validation.
- `test-loop-tests/src/tests/max_receipt_size.rs::test_max_receipt_size_value_return` — calls `max_receipt_size_value_return_method` to return a value of size `max_receipt_size`, which is wrapped into a `Data` receipt exceeding the limit; assert the same oversized-receipt condition.

Both tests already assert (via `borsh::object_length` on the final `Receipt`) that the size exceeds `max_receipt_size` after the fact, exactly matching the PoC idea described in the question.

### Citations

**File:** runtime/runtime/src/verifier.rs (L533-541)
```rust
    if mode == ValidateReceiptMode::NewReceipt {
        let receipt_size: u64 =
            borsh::object_length(receipt).unwrap().try_into().expect("Can't convert usize to u64");
        if receipt_size > limit_config.max_receipt_size {
            return Err(ReceiptValidationError::ReceiptSizeExceeded {
                size: receipt_size,
                limit: limit_config.max_receipt_size,
            });
        }
```

**File:** runtime/runtime/src/verifier.rs (L578-586)
```rust
    /// Used for validating older receipts that were saved in the state/received. Less strict than
    /// NewReceipt validation. Tolerates some receipts that wouldn't pass new validation. It has to
    /// be less strict because:
    /// 1) Older receipts might have been created before new validation rules.
    /// 2) There is a bug which allows to create receipts that are above the size limit. Runtime has
    ///    to handle them gracefully until the receipt size limit bug is fixed.
    ///    See https://github.com/near/nearcore/issues/12606 for details.
    ExistingReceipt,
}
```

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L124-156)
```rust
// A function call will generate a new receipt. Size of this receipt will be equal to
// `max_receipt_size`, it'll pass validation, but then `output_data_receivers` will be modified and
// the receipt's size will go above max_receipt_size. The receipt should be rejected, but currently
// isn't because of a bug (See https://github.com/near/nearcore/issues/12606)
// Runtime shouldn't die when it encounters a receipt with size above `max_receipt_size`.
#[test]
fn test_max_receipt_size_promise_return() {
    init_test_logger();

    let account = create_account_id("account0");
    let account_signer = create_user_test_signer(&account);
    let mut env = TestLoopBuilder::new()
        .enable_rpc()
        .add_user_account(&account, Balance::from_near(10_000))
        .build();

    // Deploy the test contract
    let deploy_contract_tx = SignedTransaction::deploy_contract(
        101,
        &account,
        near_test_contracts::rs_contract().into(),
        &account_signer,
        env.rpc_node().head().last_block_hash,
    );
    env.rpc_runner().run_tx(deploy_contract_tx, Duration::seconds(5));

    // User calls a contract method
    // Contract method creates a DAG with two promises: [A -then-> B]
    // When promise A is executed, it creates a third promise - `C` and does a `promise_return`.
    // The DAG changes to: [C ->then-> B]
    // The receipt for promise C is a maximum size receipt.
    // Adding the `output_data_receivers` to C's receipt makes it go over the size limit.
    let base_receipt_template = Receipt::V0(ReceiptV0 {
```

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L210-215)
```rust
/// Return a value that is as large as max_receipt_size. The value will be wrapped in a data receipt
/// and the data receipt will be bigger than max_receipt_size. The receipt should be rejected, but
/// currently isn't because of a bug (See https://github.com/near/nearcore/issues/12606)
/// Creates the following promise DAG:
/// A[self.return_large_value()] -then-> B[self.mark_test_completed()]
#[test]
```

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L350-428)
```rust
/// Assert that there was an incoming receipt with size above max_receipt_size
fn assert_oversized_receipt_occurred(node: &TestLoopNode<'_>) {
    let client = node.client();
    let chain = &client.chain;
    let epoch_manager = &*client.epoch_manager;

    let tip = chain.head().unwrap();
    let epoch_id = epoch_manager.get_epoch_id(&tip.last_block_hash).unwrap();
    let protocol_version = epoch_manager.get_epoch_protocol_version(&epoch_id).unwrap();
    let runtime_config = client.runtime_adapter.get_runtime_config(protocol_version);
    let max_receipt_size = runtime_config.wasm_config.limit_config.max_receipt_size;

    let mut block = chain.get_block(&tip.last_block_hash).unwrap();

    // Go over all blocks down to genesis looking for a receipt above max_receipt_size.
    loop {
        if block.header().is_genesis() {
            panic!("Didn't find receipt with size above max_receipt_size!");
        }
        let prev_block = chain.get_block(block.header().prev_hash()).unwrap();

        let shard_layout = epoch_manager
            .get_shard_layout(&epoch_manager.get_epoch_id(block.hash()).unwrap())
            .unwrap();

        let oversized = if ProtocolFeature::Spice.enabled(protocol_version) {
            // With spice chunks are executed asynchronously and their produced receipts are
            // persisted as receipt proofs keyed by the block in which the chunk was applied,
            // rather than as incoming receipts on the following block.
            shard_layout.shard_ids().any(|shard_id| {
                chain
                    .chain_store()
                    .iter_receipt_proofs_for_shard(block.hash(), shard_id)
                    .iter()
                    .flat_map(|proof| proof.0.iter())
                    .any(|receipt| receipt_is_oversized(receipt, max_receipt_size))
            })
        } else {
            block.chunks().iter_new().any(|new_chunk| {
                let shard_id = new_chunk.shard_id();
                let prev_shard_index = epoch_manager
                    .get_prev_shard_id_from_prev_hash(block.header().prev_hash(), shard_id)
                    .unwrap()
                    .2;
                let prev_height_included =
                    prev_block.chunks().get(prev_shard_index).unwrap().height_included();
                let incoming_receipts_proofs = get_incoming_receipts_for_shard(
                    &chain.chain_store,
                    epoch_manager,
                    shard_id,
                    &shard_layout,
                    *block.hash(),
                    prev_height_included,
                    ReceiptFilter::TargetShard,
                )
                .unwrap();
                incoming_receipts_proofs
                    .iter()
                    .flat_map(|response| response.1.iter())
                    .flat_map(|proof| proof.0.iter())
                    .any(|receipt| receipt_is_oversized(receipt, max_receipt_size))
            })
        };

        if oversized {
            return;
        }

        block = prev_block;
    }
}

fn receipt_is_oversized(receipt: &Receipt, max_receipt_size: u64) -> bool {
    let receipt_size: u64 = borsh::object_length(receipt).unwrap().try_into().unwrap();
    if receipt_size > max_receipt_size {
        tracing::info!(%receipt_size, %max_receipt_size, "found receipt above max size");
        return true;
    }
    false
```

**File:** docs/misc/state_witness_size_limits.md (L16-18)
```markdown
* `max_receipt_size - 4 MiB`:
  * All receipts must be below 4 MiB, otherwise they'll be considered invalid and rejected.
  * Previously there was no limit on receipt size. Set to 4MiB, might be reduced to 1.5MiB in the future to match the transaction limit.
```
