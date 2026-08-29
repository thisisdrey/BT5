No vulnerability found for this question.

The premise misreads the code. In `forward_from_buffer_to_shard` (congestion_control.rs:338-395), `own_congestion_info.remove_buffered_receipt_gas` is called unconditionally for every successfully forwarded receipt at line 369, inside the `ReceiptForwarding::Forwarded` match arm, symmetric to `add_buffered_receipt_gas` called unconditionally in `buffer_receipt` at line 487. [1](#0-0) [2](#0-1) 

The `should_update_outgoing_metadatas()` check only gates whether `outgoing_metadatas.update_on_receipt_popped`/`update_on_receipt_pushed` is called — that is a wholly separate bookkeeping structure (`core/store/src/trie/outgoing_metadata.rs`, used for bandwidth-scheduler request generation), not the `CongestionInfo.buffered_receipts_gas` counter. [3](#0-2) [4](#0-3) 

Since `add_buffered_receipt_gas`/`remove_buffered_receipt_gas` execution is not coupled to `should_update_outgoing_metadatas`, there is no code path where a receipt is buffered (incrementing `buffered_receipts_gas`) and later forwarded without a matching decrement. The value-conservation invariant holds for every receipt regardless of the `should_update_outgoing_metadatas` flag, so the described undercounting/permanent asymmetry is not reachable. [5](#0-4)

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L353-373)
```rust
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
```

**File:** runtime/runtime/src/congestion_control.rs (L386-393)
```rust
        for (size, gas) in outgoing_metadatas_updates {
            self.outgoing_metadatas.update_on_receipt_popped(
                buffer_shard_id,
                size,
                gas,
                state_update,
            )?;
        }
```

**File:** runtime/runtime/src/congestion_control.rs (L486-487)
```rust
        self.own_congestion_info.add_receipt_bytes(size)?;
        self.own_congestion_info.add_buffered_receipt_gas(gas)?;
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
