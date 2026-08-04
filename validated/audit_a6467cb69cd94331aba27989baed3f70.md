Found: the `System` builtin precompile (`substrate/frame/revive/src/precompiles/builtin/system.rs`) lacks an `is_delegate_call()` guard, unlike sibling precompiles (`asset-conversion`, `assets`, `vesting`, `pallet-xcm`, and the built-in `storage` precompile) which all explicitly reject `DELEGATECALL` via `env.is_delegate_call()` checks.

### Title
Missing `is_delegate_call` guard on `System` builtin precompile allows state-changing `terminate()` to execute in the caller's storage/value context via `DELEGATECALL` - (File: `substrate/frame/revive/src/precompiles/builtin/system.rs`)

### Summary
The `System` precompile at fixed address `0x900` only guards its single state-changing function, `terminate`, against `STATICCALL`/read-only context via `env.is_read_only()`, but it never checks `env.is_delegate_call()`. Every other precompile in the codebase (`AssetConversion`, `ERC20` assets precompile, `Vesting`, `pallet-xcm` precompiles, and the builtin `Storage` precompile) explicitly rejects delegate calls with `frame_support::ensure!(!env.is_delegate_call(), ...PrecompileDelegateDenied)` before dispatching, per the fix pattern documented in `prdoc/stable2606/pr_11715.prdoc`. [1](#0-0) [2](#0-1) 

### Finding Description
`pallet-revive`'s execution stack implements `delegate_call` by pushing a new frame that reuses the current contract's `account_id` (storage) and `value_transferred`, while only swapping code/caller identity via `DelegateInfo`: [3](#0-2) 

This is exactly the primitive described in the external report: `DELEGATECALL` preserves the caller's storage context and `msg.value`, so any state-changing precompile function that does not explicitly reject `is_delegate_call()` will execute against the *delegating contract's* storage/account rather than the precompile's own, letting an attacker-controlled contract route calls into it while corrupting/mutating the caller's own state under attacker-chosen conditions.

The codebase has already patched this exact class of bug for the DEX, ERC20/assets, vesting, and XCM precompiles by adding an explicit `!env.is_delegate_call()` ensure-guard at the top of `call()` (see `substrate/frame/asset-conversion/precompiles/src/lib.rs:196-199`, `substrate/frame/assets/precompiles/src/lib.rs:168-171`, `substrate/frame/vesting/precompiles/src/lib.rs:48-56`, and the builtin `storage.rs:44-51` which even flips the model — it *requires* delegate call). However, `System` (`substrate/frame/revive/src/precompiles/builtin/system.rs`) was not updated: its `call()` dispatcher checks `env.is_read_only()` only for the `terminate` selector, and never calls `env.is_delegate_call()` anywhere in the file. [4](#0-3) 

`terminate` calls `env.terminate_caller`, which schedules destruction of the current top frame's account and transfers its full native balance to a caller-supplied `beneficiary`: [5](#0-4) 

Because delegate-call frames keep `frame.account_id` equal to the delegating (calling) contract's account (only the executed *code* is swapped, per `push_frame`'s `dest: account_id` in `delegate_call`), a malicious contract can `DELEGATECALL` into the `System` precompile's `terminate` selector. The precompile has no guard preventing this, so `env.terminate_caller` operates on the *delegating contract's own account* — the caller can self-terminate (destroy) any contract that delegate-calls into a library which in turn reaches the `System` precompile, and can redirect that contract's entire native balance to an attacker-chosen `beneficiary` address, all from within contract code the victim did not directly write into the precompile call.

### Impact Explanation
`terminate` triggers `terminate_caller`, which performs an immediate balance transfer of the contract's entire native balance to `beneficiary` and schedules the account for destruction (`TerminateArgs`), as seen in `terminate_if_same_tx`/`terminate_caller` semantics elsewhere in `exec.rs`. An unprivileged attacker who convinces or tricks a contract into delegate-calling arbitrary/attacker-influenced code path (a common pattern for proxy/library contracts, similar to the "malicious contract sets up a delegatecall" primitive in the original report) can drain that contract's full native balance and destroy it — this is a direct "theft or unbacked mint or unlock" / "permanent user-fund lock" class impact against live Polkadot SDK / pallet-revive EVM-compatibility scope, requiring no admin, governance, validator, or off-chain privilege.

### Likelihood Explanation
Likelihood is high for any deployment exposing arbitrary/composable contracts through `pallet-revive`'s EVM-compatibility layer, since `DELEGATECALL` is a standard Solidity/EVM primitive freely available to any contract author, and this specific precompile is dispatched to a fixed, well-known address (`0x900`) intentionally reachable from user contracts. No race condition, front-running, or privileged actor is required — only that a delegate-caller (which could be an attacker-authored library contract that a victim's proxy pattern delegates into, or a victim tricked into using a malicious "library") reaches the precompile with `terminate` selector.

### Recommendation
Add the same guard already applied to sibling precompiles: reject `env.is_delegate_call()` unconditionally at the top of `System::call()` (mirroring `asset-conversion`, `assets`, `vesting`, and `pallet-xcm` precompiles), returning `pallet_revive::Error::<T>::PrecompileDelegateDenied`. Audit all other builtin/external precompiles for the same missing check to enforce the intended invariant that only `CALL` (and `STATICCALL` for pure view functions) may reach state-changing precompile dispatch.

### Proof of Concept
1. Deploy `VictimContract` holding native balance.
2. Deploy `MaliciousLib` whose code, when executed, calls `ISystem(0x900).terminate(attackerAddress)`.
3. `VictimContract` performs `address(MaliciousLib).delegatecall(abi.encodeCall(ISystem.terminate, (attacker)))` (e.g., because it treats `MaliciousLib` as a trusted upgrade/logic module, or because the attacker gets any code path in a proxy-style contract to delegate to attacker-supplied bytes).
4. Inside `pallet-revive`, `delegate_call` pushes a frame with `dest: VictimContract.account_id` and executes the `System` precompile's `call()` (reached because address `0x900` resolves via `AllPrecompiles`), without the delegate-call check.
5. `terminate` selector matches, `is_read_only()` is false, so `env.terminate_caller(&attackerAddress)` executes against `VictimContract`'s own account — its entire balance is transferred to `attacker` and the account is scheduled for deletion.

### Citations

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L40-55)
```rust
	fn call(
		_address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		use ISystem::ISystemCalls;
		match input {
			ISystemCalls::terminate(_) if env.is_read_only() => {
				Err(crate::Error::<T>::StateChangeDenied.into())
			},
			ISystemCalls::hashBlake256(ISystem::hashBlake256Call { input }) => {
				env.frame_meter_mut()
					.charge_weight_token(RuntimeCosts::HashBlake256(input.len() as u32))?;
				let output = sp_io::hashing::blake2_256(input.as_bytes_ref());
				Ok(output.abi_encode())
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

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L196-200)
```rust
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);

```

**File:** substrate/frame/revive/src/exec.rs (L1988-2003)
```rust
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
