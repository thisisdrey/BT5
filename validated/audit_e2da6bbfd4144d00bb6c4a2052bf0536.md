### Title
Function-call receipts can exceed `max_receipt_size` because `output_data_receivers` are appended after size validation - ([File: runtime/runtime/src/lib.rs])

### Summary
The runtime enforces a hard `max_receipt_size` limit on every newly created receipt via `validate_receipt`, but this check runs *before* the receipt's `output_data_receivers` field is populated. An ordinary contract can therefore build a promise chain whose `ReceiptIndex`-returning receipt passes size validation at (almost) exactly the limit, and then have additional `output_data_receivers` appended post-validation, pushing the final on-chain/in-witness receipt above the documented hard limit without ever failing the size check. This is the exact "insufficient size validation on attacker-controlled input" bug class described in the external HAL-27 report, reachable here through a completely unprivileged contract call.

### Finding Description
`apply_action_receipt` validates every newly generated receipt immediately after each action executes: [1](#0-0) 

This validation calls `validate_receipt(..., ValidateReceiptMode::NewReceipt)`, which enforces `receipt_size <= limit_config.max_receipt_size`: [2](#0-1) 

However, later in the same function, if the *current* action receipt has `output_data_receivers` and the function call's result is `ReturnData::ReceiptIndex`, the runtime mutates an **already-validated** new receipt by extending its `output_data_receivers` list, without re-running `validate_receipt`: [3](#0-2) 

This lets a contract construct a promise DAG (`promise_then` + `promise_return`) so that a child receipt is created at (or near) `max_receipt_size`, passes the one-time size check, and is then grown past the limit by appended `DataReceiver` entries from the parent's `output_data_receivers` — entirely under the control of an unprivileged transaction signer/contract, no elevated permissions required.

This exact defect is acknowledged in-repo (tracked as `near/nearcore#12606`) and only partially mitigated:
- `ValidateReceiptMode::ExistingReceipt` is documented as deliberately more lenient specifically to tolerate these oversized receipts: [4](#0-3) 
- The cross-shard forwarding path has to special-case and clamp the observed size so an oversized receipt does not get permanently stuck in the outgoing buffer: [5](#0-4) 
- A dedicated regression test reproduces the bug end-to-end and explicitly states the receipt "should be rejected, but currently isn't": [6](#0-5) 

The `max_receipt_size` limit exists specifically to bound the total size of `ChunkStateWitness` (target ~21 MiB, with `max_receipt_size = 4 MiB` being one of the accounted components): [7](#0-6) 

Because the check is bypassable, an oversized receipt inflates the actual witness/state size beyond what the protocol's accounting assumes, and beyond what other consumers (bandwidth scheduler, receipt sink, witness size budgeting) are designed to handle.

### Impact Explanation
This directly maps to the HAL-27 bug class ("lack of data sanitization and validation of limits" leading to oversized payloads reaching backend processing paths). Here, an unprivileged contract call can produce receipts whose real size exceeds the protocol-enforced `max_receipt_size`, which is one of several hard limits whose sum is relied upon to keep `ChunkStateWitness` within a distributable size. Multiple call sites already need special-cased workarounds (`try_forward`'s clamp, the lenient `ExistingReceipt` validation mode) to avoid receipts becoming permanently stuck or to avoid the runtime crashing when it encounters them, which is itself evidence that downstream code is not built to safely handle receipts above the stated hard limit. This is a resource/availability risk for chunk producers and validators (larger-than-expected witnesses, unaccounted size in bandwidth/congestion bookkeeping), rather than a direct funds-theft primitive.

### Likelihood Explanation
Reachable by any ordinary account with no special permissions: it only requires deploying/calling a contract that creates a `then`-chained promise DAG and calls `promise_return`, exactly as demonstrated by the existing regression test `test_max_receipt_size_promise_return`. No validator/relayer/operator privilege is needed, and the vulnerable code path (`lib.rs:1098-1116`) executes on every function call that has `output_data_receivers` and returns a `ReceiptIndex`.

### Recommendation
Re-validate (or size-account) the receipt after `output_data_receivers` are appended in `apply_action_receipt` (`runtime/runtime/src/lib.rs:1098-1116`), rejecting or failing the action if the post-mutation receipt size exceeds `limit_config.max_receipt_size`, instead of only validating pre-mutation. Remove/replace the compensating clamps in `congestion_control.rs`'s `try_forward` and the `ExistingReceipt` leniency in `verifier.rs` once the root cause is fixed, per the intent already noted against `near/nearcore#12606`.

### Proof of Concept
The existing test in-repo is a working PoC of the defect: [8](#0-7) 
It builds a promise DAG `[A -then-> B]`, has `A` create a third promise `C` and `promise_return` it (`[C -then-> B]`), sizes `C`'s receipt to exactly `max_receipt_size` before `output_data_receivers` are attached, and confirms via `assert_oversized_receipt_occurred` that the resulting on-chain receipt exceeds `max_receipt_size`, i.e., the size-limit validation was successfully bypassed by an ordinary contract call.

### Citations

**File:** runtime/runtime/src/lib.rs (L913-926)
```rust
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
```

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

**File:** docs/misc/state_witness_size_limits.md (L13-21)
```markdown
* `max_transaction_size = 1.5 MiB`
  * All transactions must be below 1.5 MiB, otherwise they'll be considered invalid and rejected.
  * Previously was 4MiB, now reduced to 1.5MiB
* `max_receipt_size - 4 MiB`:
  * All receipts must be below 4 MiB, otherwise they'll be considered invalid and rejected.
  * Previously there was no limit on receipt size. Set to 4MiB, might be reduced to 1.5MiB in the future to match the transaction limit.
* `max_receipt_total_input_size - 4 MiB + 640 B`
  * Hard limit on the combined size of a receipt's resolved promise inputs (the `ReceivedData` referenced by its `input_data_ids`). Receipts which exceed it fail with `TotalPromiseInputSizeExceeded` without executing their actions.
  * These inputs are read before `per_receipt_storage_proof_size_limit` starts counting, so without this limit a single receipt could pull `max_number_input_data_dependencies * max_receipt_size` (128 * 4 MiB) into the witness.
```
