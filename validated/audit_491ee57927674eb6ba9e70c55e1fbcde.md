### Title
Oversized outgoing receipt bypasses `max_receipt_size` check after `output_data_receivers` are appended post-validation - (File: runtime/runtime/src/verifier.rs)

### Summary
`validate_receipt` in `runtime/runtime/src/verifier.rs` computes `borsh::object_length(receipt)` and checks it against `limit_config.max_receipt_size` in `ValidateReceiptMode::NewReceipt` mode [1](#0-0) , but this check runs before `output_data_receivers` are populated on a returned promise's receipt (e.g. via `promise_return`), so a receipt that is exactly at the size limit at validation time can grow past `limit_config.max_receipt_size` once the data-receiver metadata is attached, and the oversized receipt is then propagated cross-shard. This is a known, already-tracked bug documented in the codebase as issue #12606.

### Finding Description
`validate_receipt` measures the serialized size of the receipt and rejects it if it exceeds `limit_config.max_receipt_size`, but only for `ValidateReceiptMode::NewReceipt` [1](#0-0) . An unprivileged account can deploy `near_test_contracts::rs_contract()` and invoke `max_receipt_size_promise_return_method1` with `args_size` chosen so that the "pre-output-data" receipt for the returned promise is exactly `max_receipt_size` bytes at the point `validate_receipt` runs. After that check passes, the runtime attaches `output_data_receivers` (because the promise's data receipt must be delivered to a dependent promise, e.g. the `B` in the DAG `C -then-> B`), which increases the serialized receipt size beyond `max_receipt_size`. Because the size check is not re-applied after this mutation, the oversized receipt is accepted and forwarded to the destination shard as an incoming/outgoing receipt.

This exact scenario is codified in the repository's own regression test `test_max_receipt_size_promise_return` in `test-loop-tests/src/tests/max_receipt_size.rs`, whose comment explicitly states: *"the receipt's size will go above max_receipt_size. The receipt should be rejected, but currently isn't because of a bug (See https://github.com/near/nearcore/issues/12606)"* [2](#0-1) . The test computes `args_size` so the base receipt template equals `max_receipt_size` exactly, executes `max_receipt_size_promise_return_method1`, and then calls `assert_oversized_receipt_occurred`, which walks the chain's incoming-receipt proofs / receipt proofs and asserts that a receipt whose `borsh::object_length` exceeds `max_receipt_size` was actually included in a chunk [3](#0-2) . An analogous test, `test_max_receipt_size_value_return`, reproduces the same class of bug via `return_large_value`/data-receipt wrapping [4](#0-3) .

### Impact Explanation
This falls under "state-root divergence and chain split" / "shard-halting" risk categories: chunk producers admit and include a receipt in a chunk that exceeds the hard protocol-level size bound that other validators/consumers assume is enforced. Depending on downstream handling in receiving shards or in cross-validator agreement over receipt proofs, this can produce disagreement about receipt admissibility between nodes that assume the invariant "every included receipt is ≤ max_receipt_size" holds, which is exactly the concern raised by issue #12606 that the repository's own tests were written to catch.

### Likelihood Explanation
Preconditions are minimal and fully within reach of an unprivileged attacker: fund an account, deploy `near_test_contracts::rs_contract()` (a normal, ordinary contract deployment), and call `max_receipt_size_promise_return_method1` with a computed `args_size` argument, exactly as demonstrated in the existing test [5](#0-4) . No validator, node-operator, or privileged access is required, and the test is deterministic and repeatable.

### Recommendation
Re-validate (or account for) the final serialized receipt size, including `output_data_receivers`, after all receipt-construction mutations are finished and before the receipt is admitted/forwarded — i.e., perform the `max_receipt_size` check in `validate_receipt` (or an additional check) on the fully-finalized receipt object rather than only on the pre-`output_data_receivers` intermediate form, or reserve/budget space for `output_data_receivers` in the initial size check.

### Proof of Concept
Use the existing `test-loop-tests/src/tests/max_receipt_size.rs::test_max_receipt_size_promise_return` as the reproduction: deploy `near_test_contracts::rs_contract()`, compute `args_size = max_receipt_size - base_receipt_size` where `base_receipt_size` is `borsh::object_length` of the pre-output-data-receiver `ActionReceipt` template [6](#0-5) , call `max_receipt_size_promise_return_method1` with that `args_size`, then run `assert_oversized_receipt_occurred`, which asserts `borsh::object_length(receipt) > max_receipt_size` for a receipt actually included in a chunk's incoming receipts [7](#0-6) . Expected (buggy) result: the assertion finds an oversized receipt (test passes today, confirming the bug); expected (fixed) result: `validate_receipt` should instead reject the receipt at creation with `ReceiptValidationError::ReceiptSizeExceeded`, similarly to the yield/resume case handled correctly in `test_max_receipt_size_yield_resume` [8](#0-7) .

### Citations

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

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L124-191)
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
```

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L210-267)
```rust
/// Return a value that is as large as max_receipt_size. The value will be wrapped in a data receipt
/// and the data receipt will be bigger than max_receipt_size. The receipt should be rejected, but
/// currently isn't because of a bug (See https://github.com/near/nearcore/issues/12606)
/// Creates the following promise DAG:
/// A[self.return_large_value()] -then-> B[self.mark_test_completed()]
#[test]
fn test_max_receipt_size_value_return() {
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

    let max_receipt_size = 4_194_304;

    // Call the contract
    let large_receipt_tx = SignedTransaction::call(
        102,
        account.clone(),
        account.clone(),
        &account_signer,
        Balance::ZERO,
        "max_receipt_size_value_return_method".into(),
        format!("{{\"value_size\": {}}}", max_receipt_size).into(),
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

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L271-321)
```rust
#[test]
fn test_max_receipt_size_yield_resume() {
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
    env.rpc_runner().run_tx(deploy_contract_tx, Duration::seconds(50));

    let max_receipt_size = 4_194_304;

    // Perform a yield which creates a receipt that is larger than the max_receipt_size.
    // It should be rejected because of the receipt size limit.
    let yield_receipt_tx = SignedTransaction::call(
        102,
        account.clone(),
        account.clone(),
        &account_signer,
        Balance::ZERO,
        "yield_with_large_args".into(),
        format!("{{\"args_size\": {}}}", max_receipt_size).into(),
        Gas::from_teragas(300),
        env.rpc_node().head().last_block_hash,
    );
    let yield_receipt_res =
        env.rpc_runner().execute_tx(yield_receipt_tx, Duration::seconds(10)).unwrap();

    let expected_size = 4194504;
    let expected_yield_status =
        FinalExecutionStatus::Failure(TxExecutionError::ActionError(ActionError {
            index: Some(0),
            kind: ActionErrorKind::NewReceiptValidationError(
                ReceiptValidationError::ReceiptSizeExceeded {
                    size: expected_size,
                    limit: max_receipt_size,
                },
            ),
        }));
    assert_eq!(yield_receipt_res.status, expected_yield_status);
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
