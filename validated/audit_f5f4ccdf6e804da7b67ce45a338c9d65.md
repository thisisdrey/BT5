No vulnerability found for this question.

**Analysis**: The premise mischaracterizes the code structure. `set_error` in [1](#0-0)  operates on `ActionReceiptResult`, which is a **per-receipt** aggregate created fresh via `ActionReceiptResult::new()` for each receipt being processed — it is not the shared `ChunkApplyStats` accumulator. Its `subsidized_amount` field is only ever folded in from individual actions within that single receipt via `merge` [2](#0-1) , so `set_error` only clears the contribution of the actions belonging to the one receipt currently failing.

The chunk-wide `ChunkApplyStatsV1.balance.subsidized_amount` is updated only **after** a receipt's full `ActionReceiptResult` has been finalized (post any in-receipt error handling), and the update uses `safe_add_balance` — a pure additive accumulation, not an overwrite or snapshot assignment: [3](#0-2) . Since each receipt contributes its own already-resolved `result.subsidized_amount` (which is `0` if that receipt failed, due to `set_error`, or the correct accumulated value if it succeeded) exactly once, additively, into the shared stats struct, there is no mechanism for a later-failing receipt to zero out or otherwise clobber the contributions already added by earlier, successfully-processed receipts in the same chunk.

This is confirmed by the existing test `test_one_yocto_subsidy_tracked_in_stats`, which shows two independent zero-balance receipts each contributing 1 yoctoNEAR, summing correctly to 2 in `stats.balance.subsidized_amount` [4](#0-3) . The reconciliation against `total_balance_burnt` in `chain/chain/src/runtime/mod.rs` also operates on this same final, correctly-accumulated total via `checked_sub` [5](#0-4) .

No unprivileged attacker action can cause `set_error`'s per-receipt reset to affect other receipts' already-merged contributions, since the reset scope (`ActionReceiptResult`) and the chunk-level accumulator (`ChunkApplyStatsV1.balance`) are distinct objects related only by one-way additive folding, not shared mutable state or snapshotting.

### Citations

**File:** runtime/runtime/src/lib.rs (L439-480)
```rust
    pub fn merge(&mut self, mut next_result: ActionResult) -> Result<(), RuntimeError> {
        assert!(next_result.gas_burnt_for_function_call <= next_result.gas_burnt);
        assert!(
            next_result.gas_burnt <= next_result.gas_used,
            "Gas burnt {} <= Gas used {}",
            next_result.gas_burnt,
            next_result.gas_used
        );
        self.gas_burnt = self.gas_burnt.checked_add_result(next_result.gas_burnt)?;
        self.gas_burnt_for_function_call = self
            .gas_burnt_for_function_call
            .checked_add(next_result.gas_burnt_for_function_call)
            .ok_or(IntegerOverflowError)?;
        self.gas_used = self.gas_used.checked_add_result(next_result.gas_used)?;
        self.compute_usage = safe_add_compute(self.compute_usage, next_result.compute_usage)?;
        // Profile aggregates by summing; each per-action `ActionResult`
        // contributes exactly one entry to the receipt-level contract list.
        self.profile.merge(&next_result.profile);
        self.current_contracts.push(next_result.current_contract);
        self.logs.append(&mut next_result.logs);
        match next_result.result {
            Ok(mut ret_data) => {
                if let ReturnData::ReceiptIndex(ref mut receipt_index) = ret_data {
                    // Shifting local receipt index to be global receipt index.
                    *receipt_index += self.new_receipts.len() as u64;
                }
                self.result = Ok(ret_data);
                self.new_receipts.append(&mut next_result.new_receipts);
                self.validator_proposals.append(&mut next_result.validator_proposals);
                self.tokens_burnt = self
                    .tokens_burnt
                    .checked_add(next_result.tokens_burnt)
                    .ok_or(IntegerOverflowError)?;
                self.subsidized_amount = self
                    .subsidized_amount
                    .checked_add(next_result.subsidized_amount)
                    .ok_or(IntegerOverflowError)?;
            }
            Err(err) => self.set_error(err),
        }
        Ok(())
    }
```

**File:** runtime/runtime/src/lib.rs (L482-493)
```rust
    /// Marks the receipt as failed: records the error and discards any
    /// receipt-scoped state that would otherwise leak across the failure
    /// boundary (queued receipts, proposed validators, burnt/subsidized
    /// balances). Profile, gas counters, logs and `current_contracts` are
    /// kept — they reflect work already done.
    pub fn set_error(&mut self, err: ActionError) {
        self.result = Err(err);
        self.new_receipts.clear();
        self.validator_proposals.clear();
        self.tokens_burnt = Balance::ZERO;
        self.subsidized_amount = Balance::ZERO;
    }
```

**File:** runtime/runtime/src/lib.rs (L1087-1090)
```rust
        stats.balance.tx_burnt_amount =
            safe_add_balance(stats.balance.tx_burnt_amount, tx_burnt_amount)?;
        stats.balance.subsidized_amount =
            safe_add_balance(stats.balance.subsidized_amount, result.subsidized_amount)?;
```

**File:** runtime/runtime/src/tests/apply.rs (L4869-4875)
```rust
    // The subsidy should accumulate across both receipts.
    assert_eq!(
        apply_result.stats.balance.subsidized_amount,
        Balance::from_yoctonear(2),
        "stats should track 2 yoctoNEAR subsidized across two zero-balance contract calls"
    );
}
```

**File:** chain/chain/src/runtime/mod.rs (L401-407)
```rust
        // Theoretically this may become negative but the subsidized amount is many orders
        // of magnitude lower than the burned amount for each promise, so it should not
        // happen.
        let total_balance_burnt =
            burnt.checked_sub(apply_result.stats.balance.subsidized_amount).ok_or_else(|| {
                Error::Other("subsidized amount exceeds total burnt balance".to_string())
            })?;
```
