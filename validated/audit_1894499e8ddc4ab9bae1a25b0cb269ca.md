### Title
Congestion-buffer gas accounting divergence via config-affecting protocol upgrade during buffering causes shard-halting `assert_eq!` panic - (File: `runtime/runtime/src/congestion_control.rs`)

### Summary
`ReceiptSinkV2::buffer_receipt` records `buffered_receipts_gas` using a gas value computed once at insertion time via `compute_receipt_congestion_gas(&receipt, &apply_state.config)` [1](#0-0) , while `forward_from_buffer_to_shard` removes that same accounting by recomputing gas from the stored receipt against the *current* `apply_state.config` via `receipt_congestion_gas(&receipt, &apply_state.config)` at removal time [2](#0-1) . If the two calls see different `RuntimeConfig` fee parameters (i.e., the receipt spans a config-affecting protocol upgrade while parked in the trie-backed buffer), the added and removed gas values diverge, leaving `own_congestion_info.buffered_receipts_gas()` non-zero even though every outgoing buffer is drained, which trips the `assert_eq!` in `forward_from_buffer` [3](#0-2) .

### Finding Description
`buffer_receipt` stores the receipt in the outgoing buffer using `ReceiptOrStateStoredReceipt`; when `use_state_stored_receipt` is `false`, the raw `Receipt` variant is persisted without embedding the congestion-gas metadata that was used at buffering time [4](#0-3) . Later, when the buffer drains via `forward_from_buffer_to_shard`, gas is not read back from any stored value for this variant but freshly recomputed from the receipt's content combined with `apply_state.config`, which reflects the config in force for the chunk currently being applied — not the config that was active when the receipt was buffered [5](#0-4) . If `RuntimeConfig` fee parameters that feed into the congestion-gas computation change between the insertion chunk and the removal chunk (e.g., across a protocol upgrade boundary), `remove_buffered_receipt_gas` subtracts a different value than `add_buffered_receipt_gas` added, breaking the sum-of-parts invariant. Once every per-shard buffer is empty, `forward_from_buffer` unconditionally asserts `buffered_receipts_gas() == 0`, so a residual/negative-saturated mismatch causes an unwrap-style panic that aborts chunk application for the shard.

### Impact Explanation
This matches the "shard-halting panic" category: an `assert_eq!` failure inside chunk application is a hard panic that aborts processing for that shard, not a recoverable error, causing chunk application (and thus the shard) to halt until a fix is deployed.

### Likelihood Explanation
Exploitation depends on a receipt remaining in the trie-backed outgoing-receipt buffer (which can persist across many blocks/epochs under sustained congestion) while a config-affecting protocol upgrade takes effect — a naturally occurring, periodic network event outside the attacker's direct control, though its timing is public and predictable. An unprivileged attacker only needs to submit ordinary transactions that generate outgoing receipts to a congested target shard so that some receipts land in the buffer (rather than being forwarded immediately) and remain there across the upgrade boundary; no special privileges, keys, or validator access are required. The main uncertainty is whether the vulnerable non-state-stored code path (`use_state_stored_receipt == false`) is still reachable on current mainnet state — this repository snapshot shows the branch still exists in `buffer_receipt`, but I was unable to fully confirm within available context whether `use_state_stored_receipt` is now permanently `true` for all live/reachable protocol versions, which would close the window for newly buffered receipts (legacy already-buffered raw receipts from before that feature activated could still be affected).

### Recommendation
Store the congestion gas (and size) alongside the buffered receipt for both representations — not just for `StateStoredReceipt` — and have `forward_from_buffer_to_shard` read back that stored value instead of recomputing it from `apply_state.config` at removal time, ensuring `add_buffered_receipt_gas`/`remove_buffered_receipt_gas` always operate on the identical value regardless of intervening config changes.

### Proof of Concept
1. Construct a `ReceiptSinkV2` / `TrieUpdate` test harness (as in `runtime/runtime/src/tests/apply.rs`, which already references `compute_receipt_congestion_gas`) and buffer a receipt with `apply_state.config.use_state_stored_receipt = false` under `RuntimeConfig` A, recording the gas value passed to `buffer_receipt`.
2. Mutate the `RuntimeConfig` fee parameters that feed `compute_receipt_congestion_gas`/`receipt_congestion_gas` to simulate config B (post-upgrade), constructing a new `apply_state` with `config = B`.
3. Call `forward_from_buffer_to_shard` (or the public `forward_from_buffer`) with `apply_state` using config B and sufficient outgoing gas/size limits to forward the receipt.
4. Assert that `own_congestion_info.buffered_receipts_gas()` after forwarding differs from the value added in step 1, and observe that `forward_from_buffer`'s `assert_eq!(self.sink.own_congestion_info.buffered_receipts_gas(), 0)` panics once all buffers are empty but the recomputed gas differs from the originally buffered gas.

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L281-284)
```rust
        // Assert that empty buffers match zero buffered gas.
        if all_buffers_empty {
            assert_eq!(self.sink.own_congestion_info.buffered_receipts_gas(), 0);
        }
```

**File:** runtime/runtime/src/congestion_control.rs (L292-325)
```rust
    pub(crate) fn forward_or_buffer_receipt(
        &mut self,
        receipt: Receipt,
        apply_state: &ApplyState,
        state_update: &mut TrieUpdate,
    ) -> Result<(), RuntimeError> {
        let shard = receipt.receiver_shard_id(&self.info.shard_layout)?;
        let size = compute_receipt_size(&receipt)?;
        let gas = compute_receipt_congestion_gas(&receipt, &apply_state.config)?;

        match ReceiptSinkV2::try_forward(
            receipt,
            gas,
            size,
            shard,
            &mut self.sink.outgoing_limit,
            &mut self.sink.outgoing_receipts,
            apply_state,
            &mut self.sink.stats,
        )? {
            ReceiptForwarding::Forwarded => (),
            ReceiptForwarding::NotForwarded(receipt) => {
                self.sink.buffer_receipt(
                    receipt,
                    size,
                    gas,
                    state_update,
                    shard,
                    apply_state.config.use_state_stored_receipt,
                )?;
            }
        }
        Ok(())
    }
```

**File:** runtime/runtime/src/congestion_control.rs (L344-369)
```rust
    ) -> Result<(), RuntimeError> {
        let mut num_forwarded = 0;
        let mut outgoing_metadatas_updates: Vec<(ByteSize, Gas)> = Vec::new();
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
```

**File:** runtime/runtime/src/congestion_control.rs (L465-501)
```rust
    /// Put a receipt in the outgoing receipt buffer of a shard.
    fn buffer_receipt(
        &mut self,
        receipt: Receipt,
        size: u64,
        gas: Gas,
        state_update: &mut TrieUpdate,
        shard: ShardId,
        use_state_stored_receipt: bool,
    ) -> Result<(), RuntimeError> {
        let receipt = match use_state_stored_receipt {
            true => {
                let metadata =
                    StateStoredReceiptMetadata { congestion_gas: gas, congestion_size: size };
                let receipt = StateStoredReceipt::new_owned(receipt, metadata);
                let receipt = ReceiptOrStateStoredReceipt::StateStoredReceipt(receipt);
                receipt
            }
            false => ReceiptOrStateStoredReceipt::Receipt(std::borrow::Cow::Owned(receipt)),
        };

        self.own_congestion_info.add_receipt_bytes(size)?;
        self.own_congestion_info.add_buffered_receipt_gas(gas)?;

        if receipt.should_update_outgoing_metadatas() {
            self.outgoing_metadatas.update_on_receipt_pushed(
                shard,
                ByteSize::b(size),
                gas,
                state_update,
            )?;
        }

        self.outgoing_buffers.to_shard(shard).push_back(state_update, &receipt)?;
        self.stats.buffered_receipts.entry(shard).or_default().add_receipt(size, gas);
        Ok(())
    }
```
