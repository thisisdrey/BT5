No vulnerability found for this question.

**Rationale:** The premise—that there's a "missing running-total check"—is factually incorrect. `GasCounter::prepay_gas` calls `deduct_gas(Gas::ZERO, use_gas)` [1](#0-0) , and `deduct_gas` maintains a cumulative running total via `self.promises_gas`, adding each new `use_gas` amount to the previous total (`self.promises_gas.checked_add(promises_gas)`) and checking `new_used_gas <= self.prepaid_gas` on every single call before committing the update [2](#0-1) .

This means each individual call to `promise_batch_action_function_call_weight` (or `promise_yield_create`/`promise_yield_create_with_id`) that invokes `prepay_gas(gas)` is checked against the *cumulative* sum of all gas already committed in the current execution plus burnt gas, not against `gas` in isolation [3](#0-2) . If a contract issues N calls each with `gas=X` where `N*X > prepaid_gas`, the first several calls would succeed only while the running total stays within `prepaid_gas`; the call that would push `used_gas()` (burnt + promises_gas) over `prepaid_gas` fails immediately with `GasExceeded`/`GasLimitExceeded` via `process_gas_limit` [4](#0-3) . There is no mechanism by which the sum of committed `gas` fields across receipts in a single execution can exceed `prepaid_gas`, since `promises_gas` is a running accumulator checked on every `deduct_gas`/`prepay_gas` invocation, not just once at the end. The existing unit tests in `gas_counter.rs` (`test_deduct_gas`, etc.) already exercise this accumulation logic [5](#0-4) .

### Citations

**File:** runtime/near-vm-runner/src/logic/gas_counter.rs (L126-150)
```rust
    fn deduct_gas(&mut self, gas_burnt: Gas, gas_used: Gas) -> Result<()> {
        assert!(gas_burnt <= gas_used);
        let promises_gas = gas_used.checked_sub(gas_burnt).unwrap();
        let new_promises_gas =
            self.promises_gas.checked_add(promises_gas).ok_or(HostError::IntegerOverflow)?;
        let new_burnt_gas = Gas::from_gas(self.fast_counter.burnt_gas)
            .checked_add(gas_burnt)
            .ok_or(HostError::IntegerOverflow)?;
        let new_used_gas =
            new_burnt_gas.checked_add(new_promises_gas).ok_or(HostError::IntegerOverflow)?;
        if new_burnt_gas <= self.max_gas_burnt && new_used_gas <= self.prepaid_gas {
            use std::cmp::min;
            if promises_gas != Gas::ZERO && !self.is_view {
                self.fast_counter.gas_limit = min(
                    self.max_gas_burnt.as_gas(),
                    self.prepaid_gas.checked_sub(new_promises_gas).unwrap().as_gas(),
                );
            }
            self.fast_counter.burnt_gas = new_burnt_gas.as_gas();
            self.promises_gas = new_promises_gas;
            Ok(())
        } else {
            Err(self.process_gas_limit(new_burnt_gas, new_used_gas).into())
        }
    }
```

**File:** runtime/near-vm-runner/src/logic/gas_counter.rs (L375-377)
```rust
    pub(crate) fn prepay_gas(&mut self, use_gas: Gas) -> Result<()> {
        self.deduct_gas(Gas::ZERO, use_gas)
    }
```

**File:** runtime/near-vm-runner/src/logic/gas_counter.rs (L429-435)
```rust
    #[test]
    fn test_deduct_gas() {
        let mut counter = make_test_counter(Gas::from_gas(10), Gas::from_gas(10), false);
        counter.deduct_gas(Gas::from_gas(5), Gas::from_gas(10)).expect("deduct_gas should work");
        assert_eq!(counter.burnt_gas(), Gas::from_gas(5));
        assert_eq!(counter.used_gas(), Gas::from_gas(10));
    }
```

**File:** runtime/near-vm-runner/src/logic/logic.rs (L3093-3096)
```rust
        self.pay_action_base(ActionCosts::function_call_base, sir)?;
        self.pay_action_per_byte(ActionCosts::function_call_byte, num_bytes, sir)?;
        // Prepaid gas
        self.result_state.gas_counter.prepay_gas(gas)?;
```
