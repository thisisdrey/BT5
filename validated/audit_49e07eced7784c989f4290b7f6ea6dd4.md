I found a concrete arithmetic-safety analog in the legacy WASM contracts pallet's gas metering code. This matches the bug class from the external report — an arithmetic operation whose "safe" result is never actually applied, silently producing an unexpected/incorrect state instead of a value that properly reflects the operation.### Title
Discarded `checked_sub` result in `EngineMeter::charge_ref_time` causes wasm-contract gas fuel to never be decremented - ([File: substrate/frame/contracts/src/gas.rs])

### Summary
`pallet-contracts`' `EngineMeter::charge_ref_time` performs a `checked_sub` to validate that enough engine fuel remains, but discards the `Option` result instead of writing it back into `self.fuel`. This is the same root defect flagged in the external GEB report — an arithmetic guard exists syntactically, but the computed "safe" value is never actually applied to state, so the operation's real effect (decrementing fuel) silently never happens.

### Finding Description
In `EngineMeter::charge_ref_time`: [1](#0-0) 

```rust
fn charge_ref_time(&mut self, ref_time: u64) -> Result<Syncable, DispatchError> {
    let amount = ref_time
        .checked_div(T::Schedule::get().ref_time_by_fuel())
        .ok_or(Error::<T>::InvalidSchedule)?;

    self.fuel.checked_sub(amount).ok_or_else(|| Error::<T>::OutOfGas)?;
    Ok(Syncable(self.fuel))
}
```

`self.fuel.checked_sub(amount)` returns an `Option<u64>` that is used only to `?`-propagate an `OutOfGas` error via `ok_or_else` — the computed subtraction result is never assigned back to `self.fuel`. `self.fuel` is therefore returned unchanged inside `Syncable(self.fuel)`.

This function is called from `GasMeter::sync_to_executor`, which is invoked every time control passes from the pallet's weight-based `GasMeter` back to the PolkaVM/wasm engine after a host-function call: [2](#0-1) 

The returned `Syncable` value is converted into the raw fuel `u64` handed back to the executor (`memory.set_gas`/equivalent sync point elsewhere in the pallet). Because `self.fuel` was never actually reduced by `amount`, the executor is re-armed with the same (or effectively unreduced) fuel budget on every host-function boundary, instead of the budget shrinking by the ref-time actually consumed since the last sync.

For contrast, the equivalent, newer `pallet-revive` metering code performs the operation correctly by reassigning the result: [3](#0-2) 

which shows the pattern that `pallet-contracts`' `gas.rs` fails to follow — the "safe" arithmetic call's output must be written back to the tracked state, not merely checked for validity.

### Impact Explanation
Weight/gas metering is the core mechanism that keeps a single wasm-contract call's compute bounded to what was paid for and what fits into the block's weight budget. If `self.fuel` never actually decreases through `charge_ref_time`, an executing contract can repeatedly cross host-function boundaries (any external call, storage op, event, or other syscall) while the underlying execution engine's fuel counter is effectively refreshed/reused rather than consumed. This is "public underpriced work" as called out in the acceptance criteria: unprivileged accounts calling contracts can perform far more computation per unit of charged weight than the runtime weight system assumes, which can degrade block production time (validators/collators spend more wall-clock time executing a block than its declared weight implies) and can be used to build cheap computational DoS transactions against the chain.

### Likelihood Explanation
This code path executes unconditionally on every contract host-function call boundary in `pallet-contracts` (any deployed contract calling any host function), requiring no special privilege, governance action, or malicious peer/relayer — only a normal account submitting a transaction that invokes a contract that repeatedly calls host functions in a loop. The defect is a straightforward, deterministic logic bug (discarded computation result) rather than a race condition, making it consistently reproducible.

### Recommendation
Fix `charge_ref_time` to actually apply the subtraction result to state:
```rust
fn charge_ref_time(&mut self, ref_time: u64) -> Result<Syncable, DispatchError> {
    let amount = ref_time
        .checked_div(T::Schedule::get().ref_time_by_fuel())
        .ok_or(Error::<T>::InvalidSchedule)?;

    self.fuel = self.fuel.checked_sub(amount).ok_or_else(|| Error::<T>::OutOfGas)?;
    Ok(Syncable(self.fuel))
}
```
Add a regression test asserting that repeated `charge_ref_time` calls monotonically decrease `EngineMeter::fuel` and that `OutOfGas` is returned once the budget is exhausted, matching the behavior already correctly implemented in `pallet-revive`'s `metering::weight` module.

### Proof of Concept
Conceptual reproduction (cannot be executed without a full runtime/test harness in this review):
1. Deploy a wasm contract under `pallet-contracts` whose exported entry point loops, invoking a lightweight host function (e.g., a storage read or a no-op syscall) many times within a single call.
2. Instrument/observe `EngineMeter::fuel` before and after repeated `charge_ref_time` invocations (unit-test level, mirroring the existing `gas.rs` test module) — because `self.fuel` is never reassigned, its value stays constant across calls instead of decreasing by `amount` each time.
3. Compare against `pallet-revive`'s `WeightMeter`/`EngineMeter::sync_from_executor`/`charge` path, where the analogous state (`weight_consumed`) is correctly updated via `saturating_accrue`/assignment, demonstrating the divergence and that `pallet-contracts` is charging validity-only without state effect.

Note: I was not able to trace every downstream caller of `Syncable`/`sync_to_executor` back to the exact host-function dispatch site inside this index (some `pallet-contracts` wasm-executor glue code may not be fully indexed), so the precise end-to-end weight accounting consequence in the live dispatch path should be confirmed by a full build/test run in a Devin session rather than static reading alone.

### Citations

**File:** substrate/frame/contracts/src/gas.rs (L67-76)
```rust
	/// Charge the given amount of gas.
	/// Returns the amount of fuel left.
	fn charge_ref_time(&mut self, ref_time: u64) -> Result<Syncable, DispatchError> {
		let amount = ref_time
			.checked_div(T::Schedule::get().ref_time_by_fuel())
			.ok_or(Error::<T>::InvalidSchedule)?;

		self.fuel.checked_sub(amount).ok_or_else(|| Error::<T>::OutOfGas)?;
		Ok(Syncable(self.fuel))
	}
```

**File:** substrate/frame/contracts/src/gas.rs (L247-258)
```rust
	/// Hand over the gas metering responsibility from this meter to the executor.
	///
	/// Needs to be called when leaving a host function in order to calculate how much
	/// gas needs to be charged from the **executor**. It updates the last seen executor
	/// total value so that it is correct when `sync_from_executor` is called the next time.
	///
	/// It is important that this does **not** actually sync with the executor. That has
	/// to be done by the caller.
	pub fn sync_to_executor(&mut self, before: RefTimeLeft) -> Result<Syncable, DispatchError> {
		let ref_time_consumed = before.0.saturating_sub(self.gas_left().ref_time());
		self.engine_meter.charge_ref_time(ref_time_consumed)
	}
```

**File:** substrate/frame/revive/src/metering/weight.rs (L236-248)
```rust
	pub fn sync_from_executor(&mut self, engine_fuel: polkavm::Gas) -> Result<(), DispatchError> {
		let weight_consumed = self
			.engine_meter
			.set_fuel(engine_fuel.try_into().map_err(|_| Error::<T>::OutOfGas)?);

		self.weight_consumed.saturating_accrue(weight_consumed);
		if self.weight_consumed.any_gt(self.effective_weight_limit) {
			self.weight_consumed = self.effective_weight_limit;
			return Err(<Error<T>>::OutOfGas.into());
		}

		Ok(())
	}
```
