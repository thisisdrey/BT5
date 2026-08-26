[File: 'runtime/runtime/src/congestion_control.rs'] [Function: CongestionInfo integration via ReceiptSink::own_congestion_info] Can an unprivileged attacker who fully controls a low-value account submit thousands of minimal FunctionCall receipts with zero deposit but maximal argument bytes (maximizing compute_receipt_size while minimizing action_receipt_congestion_gas) to inflate own_congestion_info.receipt_bytes far faster than delayed_receipts_gas/buffered_receipts_gas grow, exploiting an asymmetry in CongestionControl's is_fully_congested threshold (byte-based vs gas-based) that treats the shard as maximally congested purely from bytes, thereby indirectly freezing every real value-bearing refund/beneficiary transfer scheduled to or from that shard for as long as the attacker sustains byte pressure, at negligible gas cost to the attacker relative to the disruption caused? Proof idea: integration test measuring the gas cost required by an attacker to drive a shard into is_f

### Citations

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
                target:
