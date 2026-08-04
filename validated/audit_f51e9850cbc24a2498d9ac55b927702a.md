### Title
`InstantiateOrigin` permission gate is bypassed when a contract nests-instantiates another contract - ([File: substrate/frame/contracts/src/lib.rs], also [File: substrate/frame/revive/src/lib.rs])

### Summary
`pallet-contracts` and `pallet-revive` expose a `Config::InstantiateOrigin` knob that lets a runtime restrict *who* may deploy new contracts, separately from `Config::UploadOrigin` which restricts who may upload code. The restriction is enforced only at the outer, extrinsic-facing entry point (`Pallet::instantiate` / `bare_instantiate`), but the underlying `Ext::instantiate` execution primitive invoked by the WASM/PVM host function `instantiate` performs no such check at all. Any already-deployed contract can therefore instantiate arbitrary existing on-chain code hashes directly, completely sidestepping `InstantiateOrigin`, in the same way `MIMOProxyFactory.deployFor()` could be called directly to bypass `MIMOProxyRegistry`'s gating.

### Finding Description
The `Config::InstantiateOrigin` documentation explicitly states the gap: [1](#0-0) 

The dispatchable `instantiate` enforces the origin check exactly once, at the top level: [2](#0-1) 

But when a contract itself calls the `instantiate` host function (the on-chain analogue of `MIMOProxyFactory.deployFor()`), execution flows into `Ext::instantiate` inside `exec.rs`/the WASM runtime shim, which never calls `T::InstantiateOrigin::ensure_origin`: [3](#0-2) 

The same structural gap exists in `pallet-revive`, where the config doc carries an identical caveat and the gate is only checked in `bare_instantiate`, not in the nested host `instantiate` syscall handled in `vm/pvm.rs`: [4](#0-3) [5](#0-4) [6](#0-5) 

This mirrors the M-06 pattern precisely: `MIMOProxyFactory.deployFor()` had no access control of its own and relied on the expectation that only `MIMOProxyRegistry` would call it; here, `Ext::instantiate` has no access control of its own and relies on the expectation that only the outer dispatchable (or an already-restricted code hash) will reach it. Any contract whose code hash was uploaded under a permissive `UploadOrigin` can call `instantiate` on any *other* existing code hash — including code hashes originally uploaded/intended to be instantiated only by a restricted `InstantiateOrigin` — and the permission check is never consulted.

### Impact Explanation
A runtime operator who configures `InstantiateOrigin` to a restrictive origin (e.g., "only council may deploy new contracts") while leaving `UploadOrigin` permissive (a very plausible configuration, since the two knobs are documented as symmetric, independently tunable permissions) gets no actual enforcement: any signed account can upload a trivial "instantiator" contract and use it to instantiate any code hash on-chain, fully defeating the intended access control on contract deployment. This is an unauthorized-execution / origin-escalation issue at the runtime-configuration level, consistent with the "public wrappers must not widen origin" pivot.

### Likelihood Explanation
High for any runtime that relies on `InstantiateOrigin` alone (without an equally restrictive `UploadOrigin`) to gate contract deployment — the bypass requires only a signed account and the ability to upload one small helper contract, no privileged actor, relayer, or governance action needed. The only mitigation is the doc comment telling integrators to rely on `UploadOrigin` instead, which is easy to overlook given both origins are presented as first-class deployment gates.

### Recommendation
Either enforce `T::InstantiateOrigin` at the point where `Ext::instantiate` is invoked from within contract execution (attributing the origin to the calling contract/its deployer), or remove `InstantiateOrigin` as an independently meaningful knob and clearly fail configuration (e.g., via `integrity_test`) when `InstantiateOrigin` is more restrictive than `UploadOrigin`, so the two settings cannot silently diverge in a way that creates a false sense of access control.

### Proof of Concept
1. Configure a test runtime with `UploadOrigin = EnsureSigned` (anyone may upload) and `InstantiateOrigin` restricted to a single privileged account, mirroring `only_instantiation_origin_can_instantiate` in `substrate/frame/contracts/src/tests.rs` (lines 4385-4416), which shows `BOB` is rejected with `BadOrigin` when calling the `instantiate` dispatchable directly.
2. As `BOB` (non-privileged), upload a small "factory" contract via `upload_code`/`instantiate_with_code` (permitted because `UploadOrigin` is permissive) whose constructor/call function invokes the `instantiate` host API against the privileged code hash, exactly as exercised by the `instantiation_from_contract` test in `substrate/frame/contracts/src/exec.rs` (lines 2723-2800), which shows a contract successfully instantiating another code hash purely through `Ext::instantiate` with no origin check performed.
3. Observe that the new contract is deployed successfully even though `BOB` could never have called the top-level `instantiate` extrinsic directly — demonstrating the `InstantiateOrigin` gate is fully bypassed via nested instantiation, analogous to bypassing `MIMOProxyRegistry` by calling `MIMOProxyFactory.deployFor()` directly.

### Citations

**File:** substrate/frame/contracts/src/lib.rs (L426-433)
```rust
		/// Origin allowed to instantiate code.
		///
		/// # Note
		///
		/// This is not enforced when a contract instantiates another contract. The
		/// [`Self::UploadOrigin`] should make sure that no code is deployed that does unwanted
		/// instantiations.
		///
```

**File:** substrate/frame/contracts/src/lib.rs (L1061-1072)
```rust
		pub fn instantiate(
			origin: OriginFor<T>,
			#[pallet::compact] value: BalanceOf<T>,
			gas_limit: Weight,
			storage_deposit_limit: Option<<BalanceOf<T> as codec::HasCompact>::Type>,
			code_hash: CodeHash<T>,
			data: Vec<u8>,
			salt: Vec<u8>,
		) -> DispatchResultWithPostInfo {
			Migration::<T>::ensure_migrated()?;
			let origin = T::InstantiateOrigin::ensure_origin(origin)?;
			let data_len = data.len() as u32;
```

**File:** substrate/frame/contracts/src/wasm/runtime.rs (L1069-1098)
```rust
	fn instantiate(
		&mut self,
		memory: &mut [u8],
		code_hash_ptr: u32,
		weight: Weight,
		deposit_ptr: u32,
		value_ptr: u32,
		input_data_ptr: u32,
		input_data_len: u32,
		address_ptr: u32,
		address_len_ptr: u32,
		output_ptr: u32,
		output_len_ptr: u32,
		salt_ptr: u32,
		salt_len: u32,
	) -> Result<ReturnErrorCode, TrapReason> {
		self.charge_gas(RuntimeCosts::Instantiate { input_data_len, salt_len })?;
		let deposit_limit: BalanceOf<<E as Ext>::T> = if deposit_ptr == SENTINEL {
			BalanceOf::<<E as Ext>::T>::zero()
		} else {
			self.read_sandbox_memory_as(memory, deposit_ptr)?
		};
		let value: BalanceOf<<E as Ext>::T> = self.read_sandbox_memory_as(memory, value_ptr)?;
		let code_hash: CodeHash<<E as Ext>::T> =
			self.read_sandbox_memory_as(memory, code_hash_ptr)?;
		let input_data = self.read_sandbox_memory(memory, input_data_ptr, input_data_len)?;
		let salt = self.read_sandbox_memory(memory, salt_ptr, salt_len)?;
		let instantiate_outcome =
			self.ext.instantiate(weight, deposit_limit, code_hash, value, input_data, &salt);
		if let Ok((address, output)) = &instantiate_outcome {
```

**File:** substrate/frame/revive/src/lib.rs (L298-309)
```rust
		/// Origin allowed to instantiate code.
		///
		/// # Note
		///
		/// This is not enforced when a contract instantiates another contract. The
		/// [`Self::UploadOrigin`] should make sure that no code is deployed that does unwanted
		/// instantiations.
		///
		/// By default, it is safe to set this to `EnsureSigned`, allowing anyone to instantiate
		/// contract code.
		#[pallet::no_default_bounds]
		type InstantiateOrigin: EnsureOrigin<OriginFor<Self>, Success = Self::AccountId>;
```

**File:** substrate/frame/revive/src/lib.rs (L1859-1861)
```rust
		let try_instantiate = || {
			let instantiate_account = T::InstantiateOrigin::ensure_origin(origin.clone())?;

```

**File:** substrate/frame/revive/src/vm/pvm.rs (L784-790)
```rust
		match self.ext.instantiate(
			&CallResources::from_weight_and_deposit(weight, deposit_limit),
			Code::Existing(code_hash),
			value,
			input_data,
			salt.as_ref(),
		) {
```
