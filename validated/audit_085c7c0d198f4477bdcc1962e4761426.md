[1](#0-0) [2](#0-1)

### Citations

**File:** runtime/near-vm-runner/src/logic/logic.rs (L2966-2980)
```rust

        let (receipt_idx, sir) = self.promise_idx_to_receipt_idx_with_sir(promise_idx)?;
        let key = get_memory_or_register!(self, key_ptr, key_len)?;
        let key = key.into_owned();
        let value = get_memory_or_register!(self, value_ptr, value_len)?;
        let value = value.into_owned();

        self.pay_action_base(ActionCosts::deterministic_state_init_entry, sir)?;
        let bytes =
            (key.len() as u64).checked_add(value.len() as u64).ok_or(HostError::IntegerOverflow)?;
        self.pay_action_per_byte(ActionCosts::deterministic_state_init_byte, bytes, sir)?;

        self.ext.set_deterministic_state_init_data_entry(receipt_idx, action_index, key, value)?;

        Ok(())
```

**File:** runtime/near-vm-runner/src/logic/recorded_storage_counter.rs (L17-33)
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
```
