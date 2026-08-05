Audit Report

## Title
Missing `is_delegate_call` guard on `System` builtin precompile allows state-changing `terminate()` to execute in the caller's storage/value context via `DELEGATECALL` - (File: `substrate/frame/revive/src/precompiles/builtin/system.rs`)

## Summary
The `System` precompile's `call()` dispatcher only checks `env.is_read_only()` on the `terminate` selector but never checks `env.is_delegate_call()`, unlike sibling precompiles (`asset-conversion`, `assets`, `vesting`, `pallet-xcm`, builtin `storage`) which explicitly `ensure!(!env.is_delegate_call(), ...)`. [1](#0-0)  Because `delegate_call` reuses the calling frame's `account_id` and `value_transferred` when pushing the delegated frame, a contract that `DELEGATECALL`s into address `0x900` with the `terminate` selector will have its own account terminated and its balance transferred to an attacker-chosen beneficiary, rather than this being blocked as it is for other state-changing precompiles.

## Finding Description
`Stack::delegate_call` builds the new frame with `dest: account_id` where `account_id` is cloned from the *current* top frame (the delegating/caller contract), and carries over `value: top_frame.value_transferred`; only the executed code/callee identity changes via `DelegateInfo`: [2](#0-1) 

`System::call` matches `ISystemCalls::terminate`, guarding only against read-only (`STATICCALL`) context, and then calls `env.terminate_caller(&h160)` unconditionally otherwise: [3](#0-2)  There is no `is_delegate_call()` check anywhere in the file, confirmed by searching the whole file. `terminate_if_same_tx`/termination logic operates on `frame.account_id` — the *frame's* account, which for a delegate-called frame is the delegating contract's account per the code above: [4](#0-3) 

Sibling precompiles all add the missing guard, e.g. `asset-conversion`: [5](#0-4)  This confirms the codebase's intended invariant — state-changing precompile calls must reject delegate-call context — was not applied to `System::terminate`.

## Impact Explanation
An attacker who gets any victim contract to `DELEGATECALL` into code path that reaches `ISystem.terminate(beneficiary)` at `0x900` can trigger termination of the *victim's own account* (self-destruct) and redirect its entire native balance to an attacker-controlled beneficiary, with no privilege required. This matches the "theft ... or permanent user-fund lock" impact class for pallet-revive's EVM-compatibility layer.

## Likelihood Explanation
`DELEGATECALL` is a standard EVM opcode freely usable by any contract, and `0x900` is a fixed, publicly documented precompile address intentionally reachable from user contracts. No race condition, governance, or privileged actor is needed — only a victim contract that delegate-calls into attacker-influenced code (a common proxy/library pattern), making this practically exploitable and repeatable.

## Recommendation
Add `frame_support::ensure!(!env.is_delegate_call(), pallet_revive::Error::<T>::PrecompileDelegateDenied);` at the top of `System::call` (or at minimum guarding the `terminate` branch), mirroring the fix already applied in `asset-conversion`, `assets`, `vesting`, and `pallet-xcm` precompiles.

## Proof of Concept
1. Deploy `VictimContract` holding native balance.
2. Deploy `MaliciousLib` whose code calls `ISystem(0x900).terminate(attacker)`.
3. `VictimContract` performs `address(MaliciousLib).delegatecall(...)`.
4. `Stack::delegate_call` pushes a frame with `dest: VictimContract.account_id`, executing `System::call` which matches `terminate`, passes the `is_read_only()` check (call is state-changing), and calls `env.terminate_caller(&attacker)`.
5. `terminate_if_same_tx` transfers `VictimContract`'s full balance to `attacker` and schedules `VictimContract`'s account for deletion — confirmed via `frame.account_id` usage in `terminate_if_same_tx` at `substrate/frame/revive/src/exec.rs:2026-2040`, combined with delegate frame construction at `substrate/frame/revive/src/exec.rs:1982-2003`.

### Citations

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L46-49)
```rust
		match input {
			ISystemCalls::terminate(_) if env.is_read_only() => {
				Err(crate::Error::<T>::StateChangeDenied.into())
			},
```

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L96-103)
```rust
			ISystemCalls::terminate(ISystem::terminateCall { beneficiary }) => {
				// no need to adjust gas because this always deletes code
				env.frame_meter_mut()
					.charge_weight_token(RuntimeCosts::Terminate { code_removed: true })?;
				let h160 = H160::from_slice(beneficiary.as_slice());
				env.terminate_caller(&h160).map_err(Error::try_to_revert::<T>)?;
				Ok(Vec::new())
			},
```

**File:** substrate/frame/revive/src/exec.rs (L1982-2003)
```rust
		let top_frame = self.top_frame_mut();
		// Clone the contract info and apply pending storage changes so that
		// the child frame can correctly calculate storage deposit refunds.
		// See: <https://github.com/paritytech/contract-issues/issues/213>
		let mut contract_info = top_frame.contract_info().clone();
		top_frame.frame_meter.apply_pending_storage_changes(&mut contract_info);
		let account_id = top_frame.account_id.clone();
		let value = top_frame.value_transferred;
		if let Some(executable) = self.push_frame(
			FrameArgs::Call {
				dest: account_id,
				cached_info: Some(contract_info),
				delegated_call: Some(DelegateInfo {
					caller: self.caller().clone(),
					callee: address,
				}),
			},
			value,
			call_resources,
			self.is_read_only(),
			&input_data,
		)? {
```

**File:** substrate/frame/revive/src/exec.rs (L2026-2040)
```rust
		let frame = top_frame_mut!(self);
		let info = frame.contract_info();
		let trie_id = info.trie_id.clone();
		let code_hash = info.code_hash;
		let contract_address = T::AddressMapper::to_address(&frame.account_id);
		let beneficiary = T::AddressMapper::to_account_id(beneficiary);

		// balance transfer is immediate
		Self::transfer(
			&self.origin,
			&frame.account_id,
			&beneficiary,
			<Contracts<T>>::evm_balance(&contract_address),
			Preservation::Preserve,
			&mut frame.frame_meter,
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L196-200)
```rust
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);

```
