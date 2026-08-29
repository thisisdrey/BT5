### Title
Oversized-receipt clamp in `ReceiptSink::try_forward` under-charges the per-shard bandwidth budget - (File: runtime/runtime/src/congestion_control.rs)

### Summary
`try_forward` clamps `size` to `max_receipt_size` for both the admission check and the subsequent `forward_limit.size -= size` decrement, but pushes the *unclamped* receipt into `outgoing_receipts`. Since the codebase itself acknowledges (via issue #12606, referenced in the code comment) that receipts larger than `max_receipt_size` can be created (e.g. via `promise_return`, value-return, or yield/resume paths that add data after the initial size-limit check), an unprivileged attacker can produce a receipt whose true serialized size exceeds `max_receipt_size`, causing the shard's outgoing-bandwidth ledger to be charged less than the real bytes emitted.

### Finding Description
`try_forward` receives `size` computed from the true receipt (`receipt_size(&receipt)` at congestion_control.rs:352) and clamps it before comparing against `forward_limit.size` (the shard's bandwidth-granted budget for the target shard) and before decrementing that same budget: [1](#0-0) 
The comment explicitly documents this as a workaround for a known, unfixed bug (near/nearcore#12606) that "allows to create receipts that are above the size limit." The admission check and decrement then use the clamped `size`, while the full, unclamped `receipt` is pushed onto `outgoing_receipts`: [2](#0-1) 

The existing test suite (`test-loop-tests/src/tests/max_receipt_size.rs`) already demonstrates concrete, unprivileged-attacker-reachable paths that create receipts above `max_receipt_size`:
- `test_max_receipt_size_promise_return`: a promise DAG where `output_data_receivers` is appended to a receipt *after* it already passed the size check, pushing it over `max_receipt_size`. [3](#0-2) 
- `test_max_receipt_size_value_return`: a returned value wrapped into a data receipt that is bigger than `max_receipt_size`. [4](#0-3) 

Both are reachable purely by an unprivileged account deploying a contract and calling its methods with attacker-chosen `args_size`/`value_size`; the test even asserts the oversized receipt actually shows up in the incoming-receipt proofs (`assert_oversized_receipt_occurred`), confirming these oversized receipts do get forwarded/serialized onto the wire. [5](#0-4) 

Because the accounting in `try_forward` uses the clamped `size` for the `forward_limit.size -= size` decrement while the real, unclamped bytes are what get borsh-serialized into `outgoing_receipts` (and eventually into `source_receipt_proofs`), the shard can emit more real wire bytes for a given height than its `BandwidthScheduler`-granted budget allows.

### Impact Explanation
This breaks the metering invariant that every outgoing byte is charged against the granted bandwidth before being sent. In the worst case a shard could emit a chunk whose actual receipt-proof payload exceeds what receiving validators expect based on the bandwidth grant for that height, which is a determinism/consensus-adjacent congestion-control accounting bug rather than a fund-theft bug. No test evidence in the retrieved code confirms this concretely causes chunk re-validation failure or a shard halt — that consequence is plausible given the mismatch but not directly demonstrated by the code inspected here.

### Likelihood Explanation
Preconditions are cheap and entirely within reach of an unprivileged account: deploy the standard Rust test contract (`near_test_contracts::rs_contract()`), then call methods like `max_receipt_size_promise_return_method1` or `max_receipt_size_value_return_method` with attacker-chosen size parameters, as already exercised by the existing test suite. This requires no special privileges, validator access, or leaked keys — only normal transaction submission with a wasm contract. The underlying oversize-creation bug is already acknowledged as unfixed (near/nearcore#12606) in the very code being audited.

### Recommendation
Do not use the clamped `size` for the `forward_limit.size -= size` decrement or for `stats.forwarded_receipts` accounting; instead decrement the budget by the true `size` (saturating at zero if it would go negative), while still allowing forwarding of the oversized receipt to avoid it getting permanently stuck. Alternatively, fix the root cause referenced in #12606 by rejecting/validating receipt size after all fields (`output_data_receivers`, returned values, etc.) are finalized, so oversized receipts are never constructible in the first place.

### Proof of Concept
Extend `runtime/runtime/src/congestion_control.rs` (or reuse `test-loop-tests/src/tests/max_receipt_size.rs`) with a unit test that:
1. Constructs a `Receipt` whose borsh-serialized size is `max_receipt_size + N` bytes (mirroring `test_max_receipt_size_promise_return`'s output_data_receivers technique).
2. Calls `ReceiptSink::try_forward` directly with a `forward_limit.size` set to exactly `max_receipt_size`.
3. Asserts that forwarding succeeds (`ReceiptForwarding::Forwarded`) and that `forward_limit.size` after the call equals `0`, while `borsh::object_length(&receipt)` is `max_receipt_size + N`.
4. Assert `(real bytes forwarded) - (bandwidth deducted) == N > 0`, demonstrating the under-charge.
This can also be validated at the integration level by extending `test_max_receipt_size_promise_return`/`test_max_receipt_size_value_return` to additionally check the `BandwidthRequest`/granted bandwidth accounting for the receiving shard against the actual `source_receipt_proofs` byte size for that height.

### Citations

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

**File:** runtime/runtime/src/congestion_control.rs (L451-458)
```rust
        if forward_limit.gas >= admission_gas && forward_limit.size >= size {
            tracing::trace!(target: "runtime", ?shard, receipt_id=?receipt.receipt_id(), "forwarding buffered receipt");
            outgoing_receipts.push(receipt);
            forward_limit.gas = forward_limit.gas.saturating_sub(gas);
            forward_limit.size -= size;
            stats.forwarded_receipts.entry(shard).or_default().add_receipt(size, gas);

            Ok(ReceiptForwarding::Forwarded)
```

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L124-128)
```rust
// A function call will generate a new receipt. Size of this receipt will be equal to
// `max_receipt_size`, it'll pass validation, but then `output_data_receivers` will be modified and
// the receipt's size will go above max_receipt_size. The receipt should be rejected, but currently
// isn't because of a bug (See https://github.com/near/nearcore/issues/12606)
// Runtime shouldn't die when it encounters a receipt with size above `max_receipt_size`.
```

**File:** test-loop-tests/src/tests/max_receipt_size.rs (L210-212)
```rust
/// Return a value that is as large as max_receipt_size. The value will be wrapped in a data receipt
/// and the data receipt will be bigger than max_receipt_size. The receipt should be rejected, but
/// currently isn't because of a bug (See https://github.com/near/nearcore/issues/12606)
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
