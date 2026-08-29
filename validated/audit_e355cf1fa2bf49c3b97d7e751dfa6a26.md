No vulnerability found for this question.

`RecordedStorageCounter::get_storage_size()` is called only internally within `observe_size()` in [1](#0-0)  to enforce the per-receipt storage-proof size limit; its `Result<usize>` is never propagated to any refund, subsidy, or `subsidized_amount` computation. All call sites of `observe_size` (in `logic.rs` and `wasmtime_runner/logic.rs`) simply propagate the `?` error and never extract or reuse the numeric value for balance/refund arithmetic, e.g. [2](#0-1) .

The `subsidized_amount` field tracked in `ExecutionResultState`/`VMOutcome` is populated by a completely unrelated code path — the "1 yoctoNEAR on zero-balance account" promise exemption — using its own `checked_add`, not derived from storage-proof size at all: [3](#0-2) .

Separately, the apply-path re-derivation of storage-proof growth per receipt (`recorded_by_receipt`) is computed from `state_update.trie.recorded_storage_size_upper_bound()` minus a snapshot taken before the receipt, using `saturating_sub`, which is at least as strict/safe as the `checked_sub` in `RecordedStorageCounter::get_storage_size()` — it never panics, it just clamps to zero: [4](#0-3) . This is a distinct trie-level counter (`recorded_storage_size_upper_bound`), not a re-read of `RecordedStorageCounter::get_storage_size()`'s output, so there is no "safe total vs. unsafe per-field breakdown" divergence as hypothesized.

Refund arithmetic in `refund_unspent_gas_and_deposits` (`gross_gas_refund`, `unused_gas_balance_refund`, `price_deficit`/`price_surplus`) is entirely gas/price-based and uses its own `checked_add`/`checked_sub`/`safe_gas_to_balance` guards, with no dependency on the storage-proof size counter whatsoever: [5](#0-4) .

No code path exists where `get_storage_size()`'s value is used to compute a refund/subsidy total that is later re-derived downstream without matching checked arithmetic. The premise of the question is not supported by the codebase.

### Citations

**File:** runtime/near-vm-runner/src/logic/recorded_storage_counter.rs (L17-40)
```rust
    /// Update the latest observed storage proof size and check if it exceeds the limit.
    /// Should be called after every trie operation.
    pub fn observe_size(&mut self, latest_storage_proof_size: usize) -> Result<(), VMLogicError> {
        self.last_observed_storage_size = latest_storage_proof_size;

        let current_size = self.get_storage_size()?;
        if current_size > self.size_limit {
            let limit_u64 = self.size_limit.try_into().map_err(|_| {
                VMLogicError::InconsistentStateError(InconsistentStateError::IntegerOverflow)
            })?;
            return Err(VMLogicError::HostError(HostError::RecordedStorageExceeded {
                limit: ByteSize::b(limit_u64),
            }));
        }

        Ok(())
    }

    /// Get the size of storage proof that has been recorded so far by this receipt.
    pub fn get_storage_size(&self) -> Result<usize, VMLogicError> {
        self.last_observed_storage_size
            .checked_sub(self.initial_storage_size)
            .ok_or(VMLogicError::InconsistentStateError(InconsistentStateError::IntegerOverflow))
    }
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L4336-4339)
```rust
        let evicted = self.ext.storage_set(&mut self.result_state.gas_counter, &key, &value)?;
        let storage_config = &self.fees_config.storage_usage_config;
        self.recorded_storage_counter.observe_size(self.ext.get_recorded_storage_size())?;
        match evicted {
```

**File:** runtime/near-vm-runner/src/wasmtime_runner/logic.rs (L4137-4149)
```rust
    let skip_deduct = amount == Balance::from_yoctonear(1)
        && ctx.config.one_yocto_on_promise
        && ctx.result_state.current_account_balance.is_zero();
    if skip_deduct {
        ctx.result_state.subsidized_amount = ctx
            .result_state
            .subsidized_amount
            .checked_add(amount)
            .expect("subsidized_amount overflow");
    } else {
        ctx.result_state.deduct_balance(amount)?;
    }
    ctx.ext.append_action_function_call_weight(
```

**File:** runtime/runtime/src/lib.rs (L928-945)
```rust
                if let (true, Some(size_before), Some(limit)) = (
                    result.result.is_ok(),
                    storage_proof_size_before_receipt,
                    storage_proof_limit_for_all_actions,
                ) {
                    let recorded_by_receipt = state_update
                        .trie
                        .recorded_storage_size_upper_bound()
                        .saturating_sub(size_before);
                    if recorded_by_receipt > limit {
                        result.set_error(
                            ActionErrorKind::ReceiptStorageProofSizeExceeded {
                                limit: limit as u64,
                            }
                            .into(),
                        );
                    }
                }
```

**File:** runtime/runtime/src/lib.rs (L1249-1296)
```rust
        let deposit_refund = if result.result.is_err() { total_deposit } else { Balance::ZERO };
        let gross_gas_refund = if result.result.is_err() {
            prepaid_gas
                .checked_add(prepaid_exec_gas.gas)
                .ok_or(IntegerOverflowError)?
                .checked_sub(result.gas_burnt)
                .unwrap()
        } else {
            prepaid_gas
                .checked_add(prepaid_exec_gas.gas)
                .ok_or(IntegerOverflowError)?
                .checked_sub(result.gas_used)
                .unwrap()
        };

        // NEP-536 also adds a penalty to gas refund.
        let refund_penalty: Gas = config.fees.gas_penalty_for_gas_refund(gross_gas_refund);
        let penalty_gas_price = if ProtocolFeature::AccountCostIncrease.enabled(protocol_version) {
            gas_burn_price
        } else {
            gas_purchase_price
        };
        let refund_penalty_amount = safe_gas_to_balance(penalty_gas_price, refund_penalty)?;

        // Refund for the leftover gas that was not used by this receipt.
        let unused_gas_balance_refund = safe_gas_to_balance(gas_purchase_price, gross_gas_refund)?
            .saturating_sub(refund_penalty_amount);

        let mut gas_refund_result = GasRefundResult {
            price_deficit: Balance::ZERO,
            price_surplus: Balance::ZERO,
            refund_penalty: refund_penalty_amount,
            create_account_charge: Balance::ZERO,
        };

        if gas_burn_price > gas_purchase_price {
            // price increased, burning resulted in a deficit
            gas_refund_result.price_deficit = safe_gas_to_balance(
                gas_burn_price.checked_sub(gas_purchase_price).unwrap(),
                result.gas_burnt,
            )?;
        } else {
            // price decreased, burning resulted in a surplus
            gas_refund_result.price_surplus = safe_gas_to_balance(
                gas_purchase_price.checked_sub(gas_burn_price).unwrap(),
                result.gas_burnt,
            )?;
        };
```
