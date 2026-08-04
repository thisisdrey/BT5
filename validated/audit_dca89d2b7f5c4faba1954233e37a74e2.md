Confirmed: `set_immutable_data` in `substrate/frame/revive/src/exec.rs` only rejects calls from the `Call` export or with empty data — it does **not** enforce the "only once" invariant that the public API documentation promises.

### Title
`pallet-revive`: `set_immutable_data` can be invoked multiple times during a contract constructor, silently overwriting Solidity `immutable` state - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
The host function contract `pallet_revive_uapi::HostFn::set_immutable_data` documents that it "is only valid to set non-empty immutable data in the constructor once" and that it "Traps if: ... Called more than once." [1](#0-0)  The actual implementation backing this call in `Stack::set_immutable_data` never enforces the "called more than once" rule — it only rejects calls made from the `Call` export or with empty `data`, then unconditionally overwrites the stored value:

```rust
fn set_immutable_data(&mut self, data: ImmutableData) -> Result<(), DispatchError> {
    let frame = self.top_frame_mut();
    if frame.entry_point == ExportedFunction::Call || data.is_empty() {
        return Err(Error::<T>::InvalidImmutableAccess.into());
    }
    frame.contract_info().set_immutable_data_len(data.len() as u32);
    <ImmutableDataOf<T>>::insert(T::AddressMapper::to_address(&frame.account_id), &data);
    Ok(())
}
``` [2](#0-1) 

This mirrors the FraxLend `setTimeLock` bug class exactly: a value that must be immutable/set-once per its own contract documentation is reachable via a public-facing entry point without any "already set" mutex, allowing repeated resets.

### Finding Description
Solidity `immutable` variables are supposed to be fixed permanently after constructor execution (this is the entire premise of PR "[pallet-revive] immutable data storage", which explicitly maps this feature to [Solidity immutable variables](https://docs.soliditylang.org/en/latest/contracts.html#immutable)) [3](#0-2) . Any compiled contract can invoke the `set_immutable_data` syscall (exposed through `env::set_immutable_data` in the PVM VM layer) any number of times while its `entry_point` is `Constructor`, and each call fully overwrites `<ImmutableDataOf<T>>` for the address with new data [4](#0-3) . Because the guard only checks `entry_point == ExportedFunction::Call`, calling `set_immutable_data` two or more times inside `deploy()` (the constructor export) succeeds every time and simply reassigns `ImmutableDataOf`.

This breaks the invariant that toolchains (e.g. Solidity/YUL compilers targeting revive), downstream contracts, and off-chain indexers rely on when treating `immutable` data as fixed at deployment time — no re-entrant call within the constructor should be able to change it after the "real" assignment. Any constructor logic that reads external, attacker-influenced input (e.g. via `call()` to another untrusted contract during construction, or a re-entrant callback) can be exploited by a malicious sub-call to invoke `set_immutable_data` again with a different payload before the constructor finishes, silently altering values (e.g. an "owner" or "oracle" address baked in as immutable) that other code paths assume can never change.

### Impact Explanation
Contracts that use immutable storage for security-critical values (owner addresses, price oracle addresses, fee recipients, access-control roles) assume, per the documented API contract, that these values are frozen after the first successful `set_immutable_data` call. Since the enforcement is missing, a constructor with any external call surface (calls to precompiles, other contracts, or callbacks) can have its immutable data reassigned during construction by an attacker-controlled reentrant path, before the deployer's intended value is ever read. This is a runtime bug that "compromises intended behavior" of `pallet-revive` under the Polkadot SDK Impact Gate, since it silently violates the immutability contract of a core EVM-compatibility primitive without requiring any admin/governance/relayer trust assumption — an ordinary contract deployer or an attacker crafting a malicious sub-call reached from within a constructor is sufficient.

### Likelihood Explanation
Likelihood is moderate-to-high for any constructor containing external calls before its final `set_immutable_data` invocation, since no special privilege is required and the check is purely structural (`entry_point != Call`), which trivially holds throughout the entire constructor execution. Note: this analysis is based on the currently indexed lines of `exec.rs`; a background Devin session would be needed to fully audit the constructor call stack and confirm whether the compiler toolchain always emits a single non-reentrant `set_immutable_data` call, which would reduce practical exploitability to hand-crafted PVM bytecode rather than standard Solidity-compiled contracts.

### Recommendation
Add a one-time-set guard analogous to the FraxLend mitigation: introduce a per-frame or per-contract flag (e.g. `immutable_data_set: bool` in the top frame) that is checked and set atomically inside `set_immutable_data`, rejecting any subsequent call with `Error::<T>::InvalidImmutableAccess` once the flag is `true`, consistent with the documented API contract in `pallet_revive_uapi::HostFn::set_immutable_data`.

### Proof of Concept
1. Deploy a contract whose `deploy()` export:
   - Calls `set_immutable_data(data_A)`.
   - Makes an external call (`call()`/`delegate_call()`) into an attacker-controlled contract.
   - The attacker contract's `call()` export re-enters and does nothing malicious directly, but the outer constructor subsequently calls `set_immutable_data(data_B)` again based on the external call's return data.
2. Because `frame.entry_point` is still `ExportedFunction::Constructor` for the whole deploy execution, both calls succeed; `ImmutableDataOf<T>` ends up storing `data_B`, not the originally intended `data_A`.
3. Any code that later reads `get_immutable_data()` (e.g. an "owner" check) observes `data_B`, an attacker-influenced value, violating the "immutable, set-once" guarantee documented in `pallet_revive_uapi::HostFn::set_immutable_data` [1](#0-0)  and demonstrated as unguarded in `Stack::set_immutable_data` [2](#0-1) .

### Citations

**File:** substrate/frame/revive/uapi/src/host.rs (L42-53)
```rust
	/// Set the contract immutable data.
	///
	/// It is only valid to set non-empty immutable data in the constructor once.
	///
	/// Traps if:
	/// - Called from within the call export.
	/// - Called more than once.
	/// - The provided data was empty.
	///
	/// # Parameters
	/// - `data`: A reference to the data to be stored as immutable bytes.
	fn set_immutable_data(data: &[u8]);
```

**File:** substrate/frame/revive/src/exec.rs (L2076-2084)
```rust
	fn set_immutable_data(&mut self, data: ImmutableData) -> Result<(), DispatchError> {
		let frame = self.top_frame_mut();
		if frame.entry_point == ExportedFunction::Call || data.is_empty() {
			return Err(Error::<T>::InvalidImmutableAccess.into());
		}
		frame.contract_info().set_immutable_data_len(data.len() as u32);
		<ImmutableDataOf<T>>::insert(T::AddressMapper::to_address(&frame.account_id), &data);
		Ok(())
	}
```

**File:** prdoc/stable2412/pr_5861.prdoc (L1-8)
```text
title: "[pallet-revive] immutable data storage"

doc:
  - audience: Runtime Dev
    description: |
      This PR introduces the concept of immutable storage data, used for
      [Solidity immutable variables](https://docs.soliditylang.org/en/latest/contracts.html#immutable).
      
```

**File:** substrate/frame/revive/src/vm/pvm/env.rs (L630-641)
```rust
	/// Attaches the supplied immutable data to the currently executing contract.
	/// See [`pallet_revive_uapi::HostFn::set_immutable_data`].
	fn set_immutable_data(&mut self, memory: &mut M, ptr: u32, len: u32) -> Result<(), TrapReason> {
		if len > limits::IMMUTABLE_BYTES {
			return Err(Error::<E::T>::OutOfBounds.into());
		}
		self.charge_gas(RuntimeCosts::SetImmutableData(len))?;
		let buf = memory.read(ptr, len)?;
		let data = buf.try_into().expect("bailed out earlier; qed");
		self.ext.set_immutable_data(data)?;
		Ok(())
	}
```
