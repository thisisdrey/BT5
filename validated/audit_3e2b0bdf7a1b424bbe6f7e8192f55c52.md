This is a confirmed, already-known bug in the codebase, explicitly referenced by both the source comments and an existing test.

### Title
Oversized action receipts bypass `max_receipt_size` hard limit via post-validation `output_data_receivers` mutation - (File: runtime/runtime/src/lib.rs)

### Summary
`Runtime::process_action_receipt` calls `validate_receipt(..., ValidateReceiptMode::NewReceipt)` on newly created receipts, which measures the borsh-serialized size and enforces `max_receipt_size`, but afterward extends the chosen receipt's `output_data_receivers` with the parent's output data receivers without re-validating size. An unprivileged contract using `promise_create`+`promise_return` can therefore produce a receipt that passes validation at exactly `max_receipt_size` and then grows beyond the limit once `output_data_receivers` is appended, and this oversized receipt is placed into `outgoing_receipts`/incoming receipts un-truncated.

### Finding Description
In `Runtime::process_action_receipt`, after action execution, if `result.result` is `ReturnData::ReceiptIndex(receipt_index)` (the `promise_return` case), the code mutates the newly-created receipt in place: `result.new_receipts.get_mut(receipt_index as usize)...output_data_receivers.extend_from_slice(&action_receipt.output_data_receivers())` [1](#0-0) . This extension happens strictly after the receipt was created and after `validate_receipt` was invoked in `ValidateReceiptMode::NewReceipt` mode, which is the only mode in which the size check (`receipt_size > limit_config.max_receipt_size` → `ReceiptValidationError::ReceiptSizeExceeded`) is performed [2](#0-1) . There is no subsequent re-validation of the receipt's size after the `output_data_receivers` mutation, so a receipt that was exactly at `max_receipt_size` before the extension ends up above the limit afterward, with no rejection.

An unprivileged account can trigger this deterministically: deploy a contract implementing a promise DAG `A -then-> B` where `A` internally creates promise `C` and calls `promise_return`, changing the DAG to `C -then-> B` (matching `max_receipt_size_promise_return_method1/2` in the test contract). By sizing the call arguments so `C`'s receipt is exactly `max_receipt_size` bytes at the moment `validate_receipt` runs, and since `B`'s data receiver gets appended to `C`'s receipt via the `promise_return` mechanism, `C`'s final receipt size exceeds `max_receipt_size` with no error raised.

This is a known, already-acknowledged bug in the codebase itself: the runtime code contains explicit workaround comments referencing `https://github.com/near/nearcore/issues/12606` in `congestion_control.rs`'s `try_forward` (clamps `size` down to `max_receipt_size` purely for outgoing-limit accounting) [3](#0-2)  and in `generate_bandwidth_request`'s `get_receipt_group_sizes_for_buffer_to_shard` sizing logic (clamps bandwidth-request sizes) [4](#0-3) . These clamps only affect admission/scheduling bookkeeping — the actual `Receipt` object stored in `outgoing_receipts` and consumed as an incoming receipt by the next chunk is never truncated or re-validated, so the oversized receipt is genuinely included in `ApplyResult`/`outgoing_receipts`.

The existing test `test_max_receipt_size_promise_return` in `test-loop-tests/src/tests/max_receipt_size.rs` reproduces exactly this scenario end-to-end (deploys the real contract, computes `args_size` so that `C`'s receipt size equals `max_receipt_size` before the `output_data_receivers` extension, executes it through the RPC/runtime pipeline, and then asserts via `assert_oversized_receipt_occurred`/`receipt_is_oversized` that an actual incoming/chunk receipt exceeds `max_receipt_size`) [5](#0-4) [6](#0-5) . The test's own doc comment explicitly states: "the receipt's size will go above max_receipt_size. The receipt should be rejected, but currently isn't because of a bug" [7](#0-6) .

### Impact Explanation
This violates the documented hard invariant that `ReceiptSizeExceeded` caps all receipts at `max_receipt_size`. Because both `try_forward`/congestion control and bandwidth-request generation only clamp size for their own local accounting (never touching the underlying receipt bytes), a chunk producer can legitimately include an oversized receipt in `outgoing_receipts`, and any downstream shard's chunk producer/validator processes it as an "incoming receipt." If any validator implementation (current or future, including alternative/independently-implemented clients) enforces `max_receipt_size` strictly when re-validating incoming receipts rather than relying on the same clamping workaround, this creates a state-transition/consensus disagreement basis between clients — a latent chain-split risk category. It does not directly cause fund theft, but it undermines metering totality guarantees the size limit is meant to enforce, and is explicitly flagged in-repo as a correctness bug (`nearcore#12606`), not a hardening suggestion.

### Likelihood Explanation
Preconditions are minimal: any funded account can deploy a wasm contract and call it; no validator/node privilege is required. The exploit is fully deterministic and repeatable — the exact `args_size` needed to hit `max_receipt_size` before the `output_data_receivers` extension can be computed offline via `borsh::object_length`, as shown in the existing test's `base_receipt_size`/`args_size` computation [8](#0-7) . Cost to the attacker is just gas/storage for one contract deployment and two transaction calls.

### Recommendation
Re-validate (or re-measure and reject/truncate) the receipt's size in `Runtime::process_action_receipt` immediately after the `output_data_receivers.extend_from_slice` mutation, before the receipt is added to `result.new_receipts`/`outgoing_receipts`, returning `ReceiptValidationError::ReceiptSizeExceeded` (or failing the originating action) if the post-mutation size exceeds `max_receipt_size`, rather than relying on best-effort clamping solely in congestion-control accounting paths.

### Proof of Concept
Use the existing `test_max_receipt_size_promise_return` test in `test-loop-tests/src/tests/max_receipt_size.rs` [9](#0-8)  as the PoC: it deploys `near_test_contracts::rs_contract()`, computes `args_size = max_receipt_size - base_receipt_size`, calls `max_receipt_size_promise_return_method1`, then calls `assert_oversized_receipt_occurred` which scans chunks/incoming receipts back to genesis and asserts via `receipt_is_oversized` (`borsh::object_length(receipt) > max_receipt_size`) that a receipt genuinely included in a produced chunk's incoming receipts exceeds the limit [10](#0-9) . Running this test currently passes (i.e., it currently detects the oversized receipt rather than failing to find one), confirming the vulnerability is live and reachable through ordinary transaction submission.

### Citations

**File:** runtime/runtime/src/lib.rs (L1098-1116)
```rust
        if !action_receipt.output_data_receivers().is_empty() {
            if let Ok(ReturnData::ReceiptIndex(receipt_index)) = result.result {
                // Modifying a new receipt instead of sending data
                match result
                    .new_receipts
                    .get_mut(receipt_index as usize)
                    .expect("the receipt for the given receipt index should exist")
                    .receipt_mut()
                {
                    ReceiptEnum::Action(new_action_receipt)
                    | ReceiptEnum::PromiseYield(new_action_receipt) => new_action_receipt
                        .output_data_receivers
                        .extend_from_slice(&action_receipt.output_data_receivers()),
                    ReceiptEnum::ActionV2(new_action_receipt)
                    | ReceiptEnum::PromiseYieldV2(new_action_receipt) => new_action_receipt
                        .output_data_receivers
                        .extend_from_slice(&action_receipt.output_data_receivers()),
                    _ => unreachable!("the receipt should be an action receipt"),
                }
```

**File:** runtime/runtime/src/verifier.rs (L533-542)
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
    }
```

**File:** runtime/runtime/src/congestion_control.rs (L413-427)
```rust
        // There is a bug which allows to create receipts that are above the size limit. Receipts
        // above the size limit might not fit under the maximum outgoing size limit. Let's pretend
        // that all receipts are at most `max_receipt_size` to avoid receipts getting stuck.
        // See https://github.com/near/nearcore/issues/12606
        let max_receipt_size = apply_state.config.wasm_config.limit_config.max_receipt_size;
        if size > max_receipt_size {
            tracing::debug!(
                target: "runtime",
                receipt_id=?receipt.receipt_id(),
                size,
                max_receipt_size,
                "try_forward observed a receipt with size exceeding the size limit",
            );
            size = max_receipt_size;
        }
```

**File:** runtime/runtime/src/congestion_control.rs (L556-562)
```rust
        // There's a bug which allows to create receipts above `max_receipt_size` (https://github.com/near/nearcore/issues/12606).
        // This could cause problems with bandwidth scheduler which would generate requests for size above max size, and these
        // requests would never be fulfilled. For bandwidth requests let's pretend that all sizes are below `max_receipt_size`.
        // The same pretending logic is also present in `try_forward` which compares receipt size with outgoing limit.
        // This logic should also make it possible to do protocol upgrades that lower `max_receipt_size` without too much trouble.
        let sizes_iter = receipt_sizes_iter
            .map_ok(|group_size| std::cmp::min(group_size, params.max_receipt_size));
```

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L124-208)
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
        predecessor_id: account.clone(),
        receiver_id: account.clone(),
        receipt_id: CryptoHash::default(),
        receipt: ReceiptEnum::Action(ActionReceipt {
            signer_id: account.clone(),
            signer_public_key: account_signer.public_key().into(),
            gas_price: Balance::ZERO,
            output_data_receivers: vec![],
            input_data_ids: vec![],
            actions: vec![Action::FunctionCall(Box::new(FunctionCallAction {
                method_name: "noop".into(),
                args: vec![],
                gas: Gas::ZERO,
                deposit: Balance::ZERO,
            }))],
        }),
    });
    let base_receipt_template = action_receipt_v1_to_latest(&base_receipt_template);
    let base_receipt_size = borsh::object_length(&base_receipt_template).unwrap();
    let max_receipt_size = 4_194_304;
    let args_size = max_receipt_size - base_receipt_size;

    // Call the contract
    let large_receipt_tx = SignedTransaction::call(
        102,
        account.clone(),
        account.clone(),
        &account_signer,
        Balance::ZERO,
        "max_receipt_size_promise_return_method1".into(),
        format!("{{\"args_size\": {}}}", args_size).into(),
        Gas::from_teragas(300),
        env.rpc_node().head().last_block_hash,
    );
    env.rpc_runner().run_tx(large_receipt_tx, Duration::seconds(5));

    // Make sure that the last promise in the DAG was called
    let assert_test_completed = SignedTransaction::call(
        103,
        account.clone(),
        account,
        &account_signer,
        Balance::ZERO,
        "assert_test_completed".into(),
        "".into(),
        Gas::from_teragas(300),
        env.rpc_node().head().last_block_hash,
    );
    env.rpc_runner().run_tx(assert_test_completed, Duration::seconds(5));

    assert_oversized_receipt_occurred(&env.validator());
}
```

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L350-429)
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
}
```
