### Title
Oversized-receipt clamp in `try_forward` desynchronizes `own_congestion_info`/`OutgoingMetadatas` byte accounting from the per-shard `OutgoingLimit.size` budget - ([File: runtime/runtime/src/congestion_control.rs])

### Summary
`ReceiptSinkV2::try_forward` clamps an oversized receipt's `size` to `max_receipt_size` in a local `mut size` parameter (lines 403-427) to work around issue [#12606](https://github.com/near/nearcore/issues/12606) (receipts that can be created above the configured max size). Because this clamped value never propagates back to the caller, `forward_from_buffer_to_shard` still uses the original unclamped `size` (computed via `receipt_size(&receipt)` at line 352) for `own_congestion_info.remove_receipt_bytes(size)` (line 368) and for the `outgoing_metadatas.update_on_receipt_popped` update (lines 386-392), while the actual per-shard `forward_limit.size` inside `try_forward` is decremented only by the clamped `max_receipt_size` (line 455).

### Finding Description
`try_forward` receives `size` by value as `mut size: u64` (congestion_control.rs:406) and internally reassigns it to `max_receipt_size` when the receipt is oversized (congestion_control.rs:417-427). This local reassignment is invisible to the caller. In `forward_from_buffer_to_shard` (congestion_control.rs:338-395), the outer `size` variable used to compute `outgoing_metadatas_updates.push((ByteSize::b(size), gas))` (line 372) and `own_congestion_info.remove_receipt_bytes(size)` (line 368) is the pre-clamp, true on-disk receipt size read at line 352 (`receipt_size(&receipt)?`). Meanwhile, `forward_limit.size -= size` inside `try_forward` (line 455) subtracts only the clamped value from the per-shard `OutgoingLimit` budget. The two bookkeeping mechanisms are supposed to reflect the same physical bytes leaving the buffer, but they diverge by `(true_size - max_receipt_size)` on every oversized receipt that is popped from the buffer and forwarded. The full, non-truncated `receipt` object (with its true size) is still pushed into `outgoing_receipts` (line 453) and actually transmitted, so real bytes sent exceed what `OutgoingLimit.size` "charges" for. Existing checks (nonce/access-key/gas metering/size limits at receipt-creation time) do not prevent this because the divergence occurs entirely in the internal chunk-apply bookkeeping after a receipt is already buffered — this is a bookkeeping/invariant bug, not an authorization bypass. Reachability depends on the pre-existing #12606 condition (an oversized receipt reaching the buffer in the first place), which the code comment itself documents as a known, already-tracked bug.

### Impact Explanation
The concrete, scoped impact is congestion/bandwidth accounting divergence: `own_congestion_info`'s reported buffered/sent byte totals under-report the shard's true outgoing byte volume, and the `OutgoingLimit.size` budget for the target shard is under-decremented relative to real bytes sent. This lets a shard's real outgoing byte volume exceed the `outgoing_receipts_usual_size_limit` intended cap over repeated oversized-receipt forwards, which can accumulate and, in the worst case, push the receiving shard's chunk witness size beyond expectations set by the bandwidth scheduler/congestion control — a resource-exhaustion/availability concern rather than a direct fund-theft or state-root-divergence bug. This does not by itself cause loss/freezing of funds, double-spend, or authorization escalation.

### Likelihood Explanation
Exploitation requires first being able to trigger the underlying #12606 condition (a receipt whose size exceeds `max_receipt_size` reaching the outgoing buffer despite normal size validation) — this is an acknowledged pre-existing bug/gap, not newly demonstrated here. Given such a receipt gets buffered, the divergence described is deterministic and repeatable on every subsequent forward of that receipt from the buffer, with no additional attacker privilege required beyond triggering #12606 in the first place. Since the reachability of an actual oversized receipt was not independently re-derived within this investigation (it is treated in-code as a known bug reference, and I did not confirm a fresh, currently-open user-triggerable path to create such a receipt with normal transactions), the overall likelihood is conditional and not independently confirmed here.

### Recommendation
Return the clamped size from `try_forward` (e.g., return `ReceiptForwarding::Forwarded(actual_size_used)` or an out-parameter) and use that same clamped value consistently for `own_congestion_info.remove_receipt_bytes`, `remove_buffered_receipt_gas`, and `outgoing_metadatas.update_on_receipt_popped`, so all three counters (`own_congestion_info`, `OutgoingMetadatas`, `OutgoingLimit.size`) always agree on the size attributed to a forwarded receipt. Alternatively, apply the `max_receipt_size` clamp once at buffer-time (in `buffer_receipt`) so the same clamped size is stored and reused everywhere afterward, eliminating the two independent size computations at buffer-time vs. forward-time entirely.

### Proof of Concept
Unit test in `runtime/runtime/src/congestion_control.rs`:
1. Construct a `Receipt` whose serialized/computed `receipt_size` exceeds `max_receipt_size` (bypassing normal validation directly in the test, since normal construction paths reject oversized receipts).
2. Call `ReceiptSinkV2::buffer_receipt` (or the equivalent buffering path) with the true oversized `size`, and record `own_congestion_info.buffered_receipts_bytes()`/`buffered_receipts_gas()` before and after — confirm it increased by the true size.
3. Set up `outgoing_limit` for the target shard with sufficient `size`/`gas` budget.
4. Call `forward_from_buffer_to_shard` to pop and forward the receipt.
5. Assert: `own_congestion_info.buffered_receipts_bytes()` decreased by `true_size`, while `outgoing_limit[shard].size` decreased by only `max_receipt_size`; assert `true_size != max_receipt_size` to demonstrate the divergence, and assert the discrepancy equals `true_size - max_receipt_size`. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L347-393)
```rust
        for receipt_result in
            self.outgoing_buffers.to_shard(buffer_shard_id).iter(&state_update.trie, true)
        {
            let receipt = receipt_result?;
            let gas = receipt_congestion_gas(&receipt, &apply_state.config)?;
            let size = receipt_size(&receipt)?;
            let should_update_outgoing_metadatas = receipt.should_update_outgoing_metadatas();
            let receipt = receipt.into_receipt();
            let target_shard_id = receipt.receiver_shard_id(&shard_layout)?;

            match Self::try_forward(
                receipt,
                gas,
                size,
                target_shard_id,
                &mut self.outgoing_limit,
                &mut self.outgoing_receipts,
                apply_state,
                &mut self.stats,
            )? {
                ReceiptForwarding::Forwarded => {
                    self.own_congestion_info.remove_receipt_bytes(size)?;
                    self.own_congestion_info.remove_buffered_receipt_gas(gas.as_gas().into())?;
                    if should_update_outgoing_metadatas {
                        // Can't update metadatas immediately because state_update is borrowed by iterator.
                        outgoing_metadatas_updates.push((ByteSize::b(size), gas));
                    }
                    // count how many to release later to avoid modifying
                    // `state_update` while iterating based on
                    // `state_update.trie`.
                    num_forwarded += 1;
                }
                ReceiptForwarding::NotForwarded(_) => {
                    break;
                }
            }
        }

        self.outgoing_buffers.to_shard(buffer_shard_id).pop_n(state_update, num_forwarded)?;
        for (size, gas) in outgoing_metadatas_updates {
            self.outgoing_metadatas.update_on_receipt_popped(
                buffer_shard_id,
                size,
                gas,
                state_update,
            )?;
        }
```

**File:** runtime/runtime/src/congestion_control.rs (L403-463)
```rust
    fn try_forward(
        receipt: Receipt,
        gas: Gas,
        mut size: u64,
        shard: ShardId,
        outgoing_limit: &mut HashMap<ShardId, OutgoingLimit>,
        outgoing_receipts: &mut Vec<Receipt>,
        apply_state: &ApplyState,
        stats: &mut ReceiptSinkStats,
    ) -> Result<ReceiptForwarding, RuntimeError> {
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

        // Default case set to `Gas::MAX`: If no outgoing limit was defined for the receiving
        // shard, this usually just means the feature is not enabled. Or, it
        // could be a special case during resharding events. Or even a bug. In
        // any case, if we cannot know a limit, treating it as literally "no
        // limit" is the safest approach to ensure availability.
        let default_gas_limit = Gas::MAX;

        // Since bandwidth scheduler, a shard is not allowed to send any receipts if it doesn't have a grant.
        let default_size_limit = 0;

        let default_outgoing_limit =
            OutgoingLimit { gas: default_gas_limit, size: default_size_limit };
        let forward_limit = outgoing_limit.entry(shard).or_insert(default_outgoing_limit);

        let admission_gas = if ProtocolFeature::ClampOutgoingGasAdmission
            .enabled(apply_state.current_protocol_version)
        {
            gas.min(apply_state.config.congestion_control_config.allowed_shard_outgoing_gas)
        } else {
            gas
        };

        if forward_limit.gas >= admission_gas && forward_limit.size >= size {
            tracing::trace!(target: "runtime", ?shard, receipt_id=?receipt.receipt_id(), "forwarding buffered receipt");
            outgoing_receipts.push(receipt);
            forward_limit.gas = forward_limit.gas.saturating_sub(gas);
            forward_limit.size -= size;
            stats.forwarded_receipts.entry(shard).or_default().add_receipt(size, gas);

            Ok(ReceiptForwarding::Forwarded)
        } else {
            tracing::trace!(target: "runtime", ?shard, receipt_id=?receipt.receipt_id(), "not forwarding buffered receipt");
            Ok(ReceiptForwarding::NotForwarded(receipt))
        }
    }
```

**File:** core/store/src/trie/outgoing_metadata.rs (L66-80)
```rust
    /// Update the metadata when a receipt is removed from the front of the outgoing receipts buffer.
    /// Can be called only for receipts for which `update_on_receipt_pushed` was called before.
    pub fn update_on_receipt_popped(
        &mut self,
        shard_id: ShardId,
        receipt_size: ByteSize,
        receipt_gas: Gas,
        state_update: &mut TrieUpdate,
    ) -> Result<(), StorageError> {
        let metadata = self
            .metadatas
            .get_mut(&shard_id)
            .expect("Metadata for this shard should've been created when a receipt was pushed.");
        metadata.update_on_receipt_popped(receipt_size, receipt_gas, state_update)
    }
```
