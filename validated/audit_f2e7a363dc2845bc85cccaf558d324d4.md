This is a real, confirmed root-cause finding: the code at `runtime/runtime/src/congestion_control.rs:657-669` shows `receipt_congestion_gas` recomputes gas fresh via `compute_receipt_congestion_gas` for the `ReceiptOrStateStoredReceipt::Receipt` variant (used when `use_state_stored_receipt` is `false` at buffer time, per `buffer_receipt` at line 475-484), while `StateStoredReceipt` fixes the gas value in `StateStoredReceiptMetadata::congestion_gas` at buffering time (line 478). `compute_receipt_congestion_gas` (line 678-700+) derives gas from fee parameters in `RuntimeConfig`, which change across protocol upgrades. However, exploiting this requires the added and removed gas values to differ due to config change between buffer and forward, but I could not fully confirm whether `use_state_stored_receipt` is unconditionally `true` for all currently-active protocol versions (which would make the `Receipt` variant path dead code on mainnet) — this determination requires checking `ProtocolFeature`/config activation history that I did not have iteration budget to complete.

### Title
Non-deterministic congestion gas recomputation for legacy `Receipt` buffer variant can desynchronize `CongestionInfo::buffered_receipts_gas` across a protocol upgrade - ([File: runtime/runtime/src/congestion_control.rs])

### Summary
`buffer_receipt` (congestion_control.rs:466-501) stores a plain `Receipt` (not `StateStoredReceipt`) when `use_state_stored_receipt` is `false`, adding `compute_receipt_congestion_gas` computed under the config active at buffering time via `add_buffered_receipt_gas`. When later drained by `forward_from_buffer_to_shard`, `receipt_congestion_gas` (congestion_control.rs:657-669) recomputes the gas fresh from `apply_state.config` at forward time instead of reading a value fixed at buffer time, since the `Receipt` variant has no persisted metadata field.

### Finding Description
For the `ReceiptOrStateStoredReceipt::StateStoredReceipt` variant, the congestion gas is computed once and frozen into `StateStoredReceiptMetadata::congestion_gas` (congestion_control.rs:478), guaranteeing the same value is used for both `add_buffered_receipt_gas` (buffer_receipt, line 487) and `remove_buffered_receipt_gas` (forward_from_buffer_to_shard, line 369) since `receipt_congestion_gas` just reads `receipt.metadata().congestion_gas` (line 666). For the plain `Receipt` variant, no such fixed value is persisted; `receipt_congestion_gas` calls `compute_receipt_congestion_gas(receipt, config)` again using whatever `apply_state.config` is active at the time of forwarding (line 663). If `compute_receipt_congestion_gas`'s output depends on `RuntimeConfig` fee parameters (transfer/exec fees, etc.) that change between the block that buffered the receipt and the block that forwards it, the value subtracted via `remove_buffered_receipt_gas` will differ from the value that was added via `add_buffered_receipt_gas`, causing `buffered_receipts_gas.checked_sub` to potentially underflow and return `RuntimeError::UnexpectedIntegerOverflow`, which propagates out of chunk apply. [1](#0-0) [2](#0-1) [3](#0-2) 

### Impact Explanation
If reachable, this is a shard-halting liveness bug (`RuntimeError::UnexpectedIntegerOverflow` returned from `remove_buffered_receipt_gas`, which is not recoverable at the apply layer, would abort chunk application for that shard). This matches the "shard-halting panic" bounty category. However, whether this is reachable on mainnet depends entirely on whether `use_state_stored_receipt` can be `false` for any receipt that survives across a protocol-version boundary where `compute_receipt_congestion_gas`'s output changes — I was not able to confirm this in the given iteration budget.

### Likelihood Explanation
This requires: (1) `use_state_stored_receipt` being `false` at the time the receipt is buffered — this flag is itself protocol-version-gated per `RuntimeConfig`/`ProtocolFeature`, and it is very likely that `use_state_stored_receipt` has been `true` for a long time preceding the current protocol version(s) that also have working congestion control (congestion control and state-stored-receipt metadata were introduced together historically), meaning the `Receipt` (non-state-stored) code path for buffered receipts may already be dead on any live protocol version. (2) An outgoing-buffer receipt actually surviving across a protocol upgrade boundary (this can happen for congested shards). (3) `compute_receipt_congestion_gas` producing a different result for the identical receipt bytes under old vs. new `RuntimeConfig` — this is plausible for fee-affecting protocol upgrades, but not confirmed I could verify concretely from `action_receipt_congestion_gas`'s implementation within available iterations. An unprivileged attacker cannot control *when* a protocol upgrade happens, and cannot force a specific config version's fee changes; this makes the finding contingent on validator/network-controlled protocol upgrade timing (a governance/upgrade event), not solely triggerable by attacker transactions alone.

### Recommendation
Verify whether the plain `Receipt` (non-`StateStoredReceipt`) branch of `ReceiptOrStateStoredReceipt` can ever be buffered under a protocol version where `use_state_stored_receipt` is `false` while congestion control (which needs `add_buffered_receipt_gas`/`remove_buffered_receipt_gas`) is simultaneously active; if legacy support is still reachable, migrate the gas value into fixed on-buffer metadata (as done for `StateStoredReceipt`) universally, or add an explicit re-encoding/migration step for buffered receipts during protocol upgrades so `receipt_congestion_gas` never recomputes from a receipt whose originally-buffered gas value is unknown/unfixed.

### Proof of Concept
Unable to construct a concrete reproducible unit test within the available investigation because it requires confirming (a) that `use_state_stored_receipt=false` is reachable on any protocol version where congestion control buffering is also active, and (b) a concrete before/after `RuntimeConfig` pair whose `compute_receipt_congestion_gas` outputs differ for the same receipt bytes — this needs further code review (`action_receipt_congestion_gas`, `ProtocolFeature` gating of `use_state_stored_receipt`) that could not be completed in the given iteration budget. A suggested proof-of-concept skeleton: (1) construct `ReceiptSinkV2` and call `buffer_receipt` with `use_state_stored_receipt=false` and `RuntimeConfig` A; (2) swap in `RuntimeConfig` B with different `send_sir`/`exec` fee parameters into `apply_state.config`; (3) call `forward_from_buffer_to_shard`; (4) assert whether `own_congestion_info.remove_buffered_receipt_gas` underflows or `buffered_receipts_gas()` diverges from 0 after full drain, matching the debug-assert at congestion_control.rs:283.

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L475-487)
```rust
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

**File:** core/primitives/src/congestion_info.rs (L309-321)
```rust
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
