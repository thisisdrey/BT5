### Title
Congestion-control gas admission gate uses clamped `admission_gas` while the actual deduction uses the uncapped `gas`, letting an attacker drain a shard's `OutgoingLimit.gas` faster than the model allows and starve honest cross-shard receipts - ([File: runtime/runtime/src/congestion_control.rs])

### Summary
In `ReceiptSinkV2::try_forward`, the forwarding admission check at congestion_control.rs:451 compares `forward_limit.gas` against `admission_gas = gas.min(allowed_shard_outgoing_gas)`, but the subsequent decrement at congestion_control.rs:454 subtracts the full, uncapped `gas` from `forward_limit.gas` via `saturating_sub`. This mismatch lets a single (or a handful of) attacker-controlled `FunctionCall` receipts with high prepaid gas consume far more of the per-shard outgoing gas budget than the admission check accounted for, drawing down `OutgoingLimit.gas` for the target shard much faster than intended and causing honest receipts to that shard to be buffered instead of forwarded for the rest of the chunk.

### Finding Description
`try_forward` is reached from `ReceiptSinkV2WithInfo::forward_or_buffer_receipt` (congestion_control.rs:292-325), which is invoked by `ReceiptSink::forward_or_buffer_receipt` for every freshly generated outgoing receipt during chunk application [1](#0-0) . `gas` is `compute_receipt_congestion_gas(&receipt, &apply_state.config)`, i.e., the receipt's exec+send prepaid gas, which an attacker fully controls via `prepaid_gas` on a `FunctionCall` action up to `max_total_prepaid_gas` [2](#0-1) .

Inside `try_forward`:
- The admission gate is computed against a clamped value when `ClampOutgoingGasAdmission` is active: `admission_gas = gas.min(allowed_shard_outgoing_gas)` [3](#0-2) .
- The gate itself only checks `forward_limit.gas >= admission_gas && forward_limit.size >= size` [4](#0-3) .
- But once the gate passes, the code deducts the full uncapped `gas`, not `admission_gas`: `forward_limit.gas = forward_limit.gas.saturating_sub(gas)` [5](#0-4) .

This is an internal inconsistency: the gate is designed to admit a receipt whenever the shard's remaining budget covers at most `allowed_shard_outgoing_gas` worth of gas (the intended per-receipt cap), but the actual charge against the budget is the receipt's real (uncapped) gas, which can be many multiples of `allowed_shard_outgoing_gas` for a single expensive `FunctionCall`. Nothing in the transaction/action validation path (signature, nonce, access-key, `max_total_prepaid_gas` checks) prevents an attacker from repeatedly submitting maximally-gassed `FunctionCall` receipts targeting the same shard within one chunk; those checks bound the *per-receipt* gas but do not correct this admission/deduction mismatch.

Because `saturating_sub` floors at zero, repeated (or even a single sufficiently large) oversized receipts can drive `forward_limit.gas` to 0 for shard S well before the amount of gas the admission model intended to allow has been consumed. Once at zero, honest receipts to shard S fail the `forward_limit.gas >= admission_gas` check and are routed to `buffer_receipt` (congestion_control.rs:313-322) instead of being forwarded, for the remainder of the chunk.

### Impact Explanation
This is a liveness/fairness bug in congestion control rather than a fund-theft or consensus-divergence bug: it does not corrupt state, mint tokens, or allow double-spend. It falls under availability/griefing of cross-shard receipt admission — an unprivileged attacker can unfairly monopolize (or effectively zero out) a target shard's outgoing gas budget for a chunk, causing other users' receipts to that shard to be delayed (buffered, not lost) rather than forwarded immediately. The scoped impact is temporary throughput degradation for one shard's incoming cross-shard traffic per exploited chunk; it is repeatable every chunk since nothing prevents the attacker from resubmitting.

### Likelihood Explanation
Preconditions are minimal and match an ordinary unprivileged actor: fund an account for gas fees (refunded for unused gas), deploy or call an existing contract, and issue `FunctionCall` receipts with `prepaid_gas` set high (bounded only by `max_total_prepaid_gas`) targeting an existing receiver on the victim shard. The exact magnitude of the effect (whether one receipt zeroes the limit outright, or several receipts within the chunk are needed) depends on the runtime values of `allowed_shard_outgoing_gas` versus the shard's base `outgoing_gas_limit` derived from `CongestionControl::outgoing_gas_limit`, which I could not fully confirm from the indexed snapshot in this session (the specific numeric parameter values and the `outgoing_gas_limit` implementation were not retrievable via the available search tools before the iteration limit). Regardless of exact magnitude, the code path is unconditionally reachable by any account submitting ordinary transactions, and the admission/deduction mismatch is a genuine logic defect independent of configuration values — it only affects how quickly the limit is exhausted.

### Recommendation
Make the deduction consistent with the admission check: deduct `admission_gas` (the clamped value) from `forward_limit.gas` instead of the raw `gas`, i.e., change congestion_control.rs:454 to `forward_limit.gas = forward_limit.gas.saturating_sub(admission_gas);`. This ensures the amount subtracted from the outgoing budget matches the amount that was actually verified against the remaining budget, preserving the fairness invariant that admission approximates deduction.

### Proof of Concept
Unit test in `runtime/runtime/src/congestion_control.rs` (or a test module using `ReceiptSinkV2::try_forward` directly):
1. Construct an `ApplyState`/`RuntimeConfig` with `ClampOutgoingGasAdmission` enabled and a known `allowed_shard_outgoing_gas` (e.g., `X`).
2. Initialize `outgoing_limit` for shard S with `OutgoingLimit { gas: L, size: large }` where `L` is set larger than `X` but smaller than some attacker-chosen `gas_big >> X` (e.g., `L = 2*X`, `gas_big = 10*X`).
3. Call `try_forward` with a synthetic receipt whose `gas = gas_big`, targeting shard S. Assert it returns `Forwarded` (since `admission_gas = min(gas_big, X) = X <= L`).
4. Assert `outgoing_limit[S].gas` after the call: expected-if-correct behavior would be `L - X`; actual buggy behavior is `L.saturating_sub(gas_big)`, likely `0` since `gas_big > L`.
5. Call `try_forward` again with a small honest receipt (`gas = small`, `admission_gas = small`) targeting shard S. Assert it returns `NotForwarded`, even though `small <= L - X` would have held under correct accounting — demonstrating an honest small receipt is denied admission purely due to the earlier over-deduction.

### Citations

**File:** runtime/runtime/src/congestion_control.rs (L162-174)
```rust
    pub(crate) fn forward_or_buffer_receipt(
        &mut self,
        receipt: Receipt,
        apply_state: &ApplyState,
        state_update: &mut TrieUpdate,
    ) -> Result<(), RuntimeError> {
        match self {
            ReceiptSink::V2(sink_with_info) => {
                assert_eq!(apply_state.epoch_id, sink_with_info.info.epoch_id);
                sink_with_info.forward_or_buffer_receipt(receipt, apply_state, state_update)
            }
        }
    }
```

**File:** runtime/runtime/src/congestion_control.rs (L298-300)
```rust
        let shard = receipt.receiver_shard_id(&self.info.shard_layout)?;
        let size = compute_receipt_size(&receipt)?;
        let gas = compute_receipt_congestion_gas(&receipt, &apply_state.config)?;
```

**File:** runtime/runtime/src/congestion_control.rs (L443-449)
```rust
        let admission_gas = if ProtocolFeature::ClampOutgoingGasAdmission
            .enabled(apply_state.current_protocol_version)
        {
            gas.min(apply_state.config.congestion_control_config.allowed_shard_outgoing_gas)
        } else {
            gas
        };
```

**File:** runtime/runtime/src/congestion_control.rs (L451-451)
```rust
        if forward_limit.gas >= admission_gas && forward_limit.size >= size {
```

**File:** runtime/runtime/src/congestion_control.rs (L453-454)
```rust
            outgoing_receipts.push(receipt);
            forward_limit.gas = forward_limit.gas.saturating_sub(gas);
```
