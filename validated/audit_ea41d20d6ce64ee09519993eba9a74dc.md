Both call sites use the same congestion-gas computation, `receipt_congestion_gas`/`compute_receipt_congestion_gas` (via `compute_receipt_congestion_gas` in `buffer_receipt` and `receipt_congestion_gas` in `forward_from_buffer_to_shard`), so the value passed to `add_buffered_receipt_gas` and later to `remove_buffered_receipt_gas` for the same receipt is identical — the `u128` vs `Gas` type difference is just a type-conversion detail (`gas.as_gas().into()`), not a unit or magnitude mismatch. [1](#0-0) [2](#0-1) [3](#0-2) 

Specifically:
- `buffer_receipt` computes `gas` via `compute_receipt_congestion_gas(&receipt, &apply_state.config)` at the point of buffering, then calls `add_buffered_receipt_gas(gas)`.
- `forward_from_buffer_to_shard` recomputes `gas` for the same receipt from the trie via `receipt_congestion_gas(&receipt, &apply_state.config)` (which for `StateStoredReceipt` just reads back the stored `congestion_gas` metadata that was set at buffering time, and for plain `Receipt` recomputes deterministically from the same protocol-defined formula), then calls `remove_buffered_receipt_gas(gas.as_gas().into())`.

Since the congestion-gas calculation is a deterministic, protocol-defined function of the receipt's actions and is either read back from stored metadata (`congestion_gas`) or recomputed identically, the gas value removed for a given receipt is guaranteed to equal the gas value that was added for that same receipt — there is no code path where a different or "raw yoctogas-like" value is used for removal. `forward_from_buffer_to_shard` only removes gas for receipts it actually pops (`num_forwarded`, bounded by `ReceiptForwarding::Forwarded`), so partial removal from a bandwidth-limited forward only ever subtracts gas for the receipts actually forwarded, never more than what's in the buffer. The `checked_sub` in `remove_buffered_receipt_gas` protects against underflow, but no legitimate or attacker-influenced sequence can produce a subtrahend greater than the running total because add/remove are symmetric per-receipt computations tied to the same receipt data.

An unprivileged attacker controls receipt contents (actions, gas attached, deposits) via ordinary transactions, but this only affects the magnitude of `gas` uniformly for both the add and remove calls of that receipt — it cannot desynchronize the two, since both use the same formula (`action_receipt_congestion_gas`) applied to the same receipt content. There is no crafted receipt size/gas combination that produces an add/remove unit mismatch. [4](#0-3) [5](#0-4) 

#No vulnerability found for this question.

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L351-369)
```rust
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

**File:** runtime/runtime/src/congestion_control.rs (L486-487)
```rust
        self.own_congestion_info.add_receipt_bytes(size)?;
        self.own_congestion_info.add_buffered_receipt_gas(gas)?;
```

**File:** runtime/runtime/src/congestion_control.rs (L657-669)
```rust
pub(crate) fn receipt_congestion_gas(
    receipt: &ReceiptOrStateStoredReceipt,
    config: &RuntimeConfig,
) -> Result<Gas, IntegerOverflowError> {
    match receipt {
        ReceiptOrStateStoredReceipt::Receipt(receipt) => {
            compute_receipt_congestion_gas(receipt, config)
        }
        ReceiptOrStateStoredReceipt::StateStoredReceipt(receipt) => {
            Ok(receipt.metadata().congestion_gas)
        }
    }
}
```

**File:** runtime/runtime/src/congestion_control.rs (L678-714)
```rust
pub(crate) fn compute_receipt_congestion_gas(
    receipt: &Receipt,
    config: &RuntimeConfig,
) -> Result<Gas, IntegerOverflowError> {
    match receipt.versioned_receipt() {
        VersionedReceiptEnum::Action(action_receipt) => {
            // account for gas guaranteed to be used for executing the receipts
            action_receipt_congestion_gas(receipt, config, action_receipt.into())
        }
        VersionedReceiptEnum::Data(_data_receipt) => {
            // Data receipts themselves don't cost gas to execute, their cost is
            // burnt at creation. What we should count, is the gas of the
            // postponed action receipt. But looking that up would require
            // reading the postponed receipt from the trie.
            // Thus, the congestion control MVP does not account for data
            // receipts or postponed receipts.
            Ok(Gas::ZERO)
        }
        VersionedReceiptEnum::PromiseYield(_) => {
            // The congestion control MVP does not account for yielding a
            // promise. Yielded promises are confined to a single account, hence
            // they never cross the shard boundaries. This makes it irrelevant
            // for the congestion MVP, which only counts gas in the outgoing
            // buffers and delayed receipts queue.
            Ok(Gas::ZERO)
        }
        VersionedReceiptEnum::PromiseResume(_) => {
            // The congestion control MVP does not account for resuming a promise.
            // Unlike `PromiseYield`, it is possible that a promise-resume ends
            // up in the delayed receipts queue.
            // But similar to a data receipt, it would be difficult to find the cost
            // of it without expensive state lookups.
            Ok(Gas::ZERO)
        }
        VersionedReceiptEnum::GlobalContractDistribution(_) => Ok(Gas::ZERO),
    }
}
```

**File:** core/primitives/src/congestion_info.rs (L295-321)
```rust
    pub fn add_buffered_receipt_gas(&mut self, gas: Gas) -> Result<(), RuntimeError> {
        match self {
            CongestionInfo::V1(inner) => {
                inner.buffered_receipts_gas = inner
                    .buffered_receipts_gas
                    .checked_add(gas.as_gas().into())
                    .ok_or_else(|| {
                        RuntimeError::UnexpectedIntegerOverflow("add_buffered_receipt_gas".into())
                    })?;
            }
        }
        Ok(())
    }

    pub fn remove_buffered_receipt_gas(&mut self, gas: u128) -> Result<(), RuntimeError> {
        match self {
            CongestionInfo::V1(inner) => {
                inner.buffered_receipts_gas =
                    inner.buffered_receipts_gas.checked_sub(gas).ok_or_else(|| {
                        RuntimeError::UnexpectedIntegerOverflow(
                            "remove_buffered_receipt_gas".into(),
                        )
                    })?;
            }
        }
        Ok(())
    }
```
