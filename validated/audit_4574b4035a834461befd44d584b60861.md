### Title
Post-validation mutation of `output_data_receivers` allows an unprivileged sender to create outgoing receipts exceeding `max_receipt_size` - ([File: runtime/runtime/src/verifier.rs])

### Summary
`validate_receipt` in `runtime/runtime/src/verifier.rs` computes the serialized receipt size and checks it against `max_receipt_size` *before* `ReceiptManager::create_action_receipt` appends `DataReceiver` entries to `output_data_receivers` of an already-created (and already size-checked) receipt. An unprivileged account can use a `promise_then`/`promise_return` (`promise_batch_then` + return-value pattern implemented via `create_action_receipt`) DAG to craft a receipt whose size is exactly at the limit when validated, then have the VM logic push additional `output_data_receivers` onto it afterward, producing a final on-wire receipt larger than `max_receipt_size`.

### Finding Description
`validate_receipt` (`runtime/runtime/src/verifier.rs:527-542`) serializes the receipt with `borsh::object_length` and rejects it if oversized, but only in `ValidateReceiptMode::NewReceipt` mode, and only once, at receipt-creation time. `runtime/runtime/src/receipt_manager.rs::create_action_receipt` (lines 111-137) mutates a previously created receipt's `output_data_receivers` field (`.output_data_receivers.push(...)`, lines 118-124) whenever a *new* receipt is chained to it via a promise dependency (this is the mechanism behind `promise_batch_then`/`promise_and`/`promise_return`). Because the parent receipt was already validated for size before this later `output_data_receivers` push happens, the size check is stale: an attacker can craft `args`/state so that receipt `C` is exactly at `max_receipt_size` when first validated, and only afterward gets a `DataReceiver` appended by a subsequent promise-then call, pushing the final serialized receipt above the limit. The repository itself documents this as a known, unresolved bug: the doc-comment on `ValidateReceiptMode::ExistingReceipt` explicitly references https://github.com/near/nearcore/issues/12606, stating "there is a bug which allows to create receipts that are above the size limit. Runtime has to handle them gracefully until the receipt size limit bug is fixed" (`runtime/runtime/src/verifier.rs:578-585`). This is entirely triggerable by an ordinary account: deploy a wasm contract, call a method that builds promise A→then→B, where handling A's callback issues `promise_return`/creates receipt C sized to the current limit, chaining C→then→B, which appends `output_data_receivers` to C after the fact.

### Impact Explanation
This is a Metering/size-limit-totality violation: chunk producers and validators that pre-size witnesses/state based on `max_receipt_size` can under-provision because the actual persisted/relayed receipt is larger than the protocol's declared bound. This is a resource-hygiene/liveness-adjacent issue rather than fund theft, consensus divergence, or key/authorization compromise. The existing regression tests confirm the runtime does not panic and continues operating (`test_max_receipt_size_promise_return`, `test_max_receipt_size_value_return` in `test-loop-tests/src/tests/max_receipt_size.rs`), so no chain halt or state-root divergence has been demonstrated; the issue is scoped to violating the documented size invariant, which the code already treats as a known/tracked, tolerated bug (via `ValidateReceiptMode::ExistingReceipt`) rather than a shard-halting or fund-affecting defect.

### Likelihood Explanation
Trivially reachable by any funded account: deploy the test/attacker contract, call a method that creates a `promise_then`/`promise_and` DAG where a callback issues `promise_return` to chain a new receipt onto one already near `max_receipt_size`. Cost is limited to gas/attached deposit for a function call; fully repeatable, as shown by the existing `test_max_receipt_size_promise_return` and `test_max_receipt_size_value_return` tests in `test-loop-tests/src/tests/max_receipt_size.rs:130-267`, which already reproduce this exact scenario against `near_test_contracts::rs_contract()`.

### Recommendation
Re-validate (or re-check size of) an action receipt after all mutations from dependent promise creation (`create_action_receipt`'s `output_data_receivers` push) are finalized, before it is added to `outgoing_receipts`/`ActionResult`, i.e. move/duplicate the `ReceiptSizeExceeded` check in `validate_receipt` to run after `ReceiptManager` has finished appending `output_data_receivers` for all receipts in the batch, or reserve headroom in the initial size check to account for the maximum possible number of subsequently appended `DataReceiver`s.

### Proof of Concept
Reuse the existing test `test_max_receipt_size_promise_return` in `test-loop-tests/src/tests/max_receipt_size.rs:130-208`: deploy `near_test_contracts::rs_contract()`, compute `args_size` so the intermediate receipt `C`'s serialized size equals `max_receipt_size` (4_194_304) before `output_data_receivers` is appended, call `max_receipt_size_promise_return_method1` to build the DAG `A -then-> B`, where `A`'s execution creates `C` and does `promise_return` (rewriting the DAG to `C -then-> B`), then assert via `assert_oversized_receipt_occurred` that an outgoing/incoming receipt with serialized size `> max_receipt_size` reaches the chain (already implemented and asserting the bug reproduces). [1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** runtime/runtime/src/verifier.rs (L527-542)
```rust
pub(crate) fn validate_receipt(
    limit_config: &LimitConfig,
    receipt: &Receipt,
    current_protocol_version: ProtocolVersion,
    mode: ValidateReceiptMode,
) -> Result<(), ReceiptValidationError> {
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

**File:** runtime/runtime/src/verifier.rs (L573-586)
```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValidateReceiptMode {
    /// Used for validating new receipts that were just created.
    /// More strict than `OldReceipt` mode, which has to handle older receipts.
    NewReceipt,
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

**File:** runtime/runtime/src/receipt_manager.rs (L111-137)
```rust
    pub(super) fn create_action_receipt(
        &mut self,
        input_data_ids: Vec<CryptoHash>,
        receipt_indices: Vec<ReceiptIndex>,
        receiver_id: AccountId,
    ) -> Result<ReceiptIndex, VMLogicError> {
        assert_eq!(input_data_ids.len(), receipt_indices.len());
        for (data_id, receipt_index) in input_data_ids.iter().zip(receipt_indices.into_iter()) {
            self.action_receipts
                .get_mut(receipt_index as usize)
                .ok_or(HostError::InvalidReceiptIndex { receipt_index })?
                .output_data_receivers
                .push(DataReceiver { data_id: *data_id, receiver_id: receiver_id.clone() });
        }

        let new_receipt = ActionReceiptMetadata {
            receiver_id,
            refund_to: None,
            output_data_receivers: vec![],
            input_data_ids,
            actions: vec![],
            is_promise_yield: false,
        };
        let new_receipt_index = self.action_receipts.len() as ReceiptIndex;
        self.action_receipts.push(new_receipt);
        Ok(new_receipt_index)
    }
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
