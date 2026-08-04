## Analysis

The Algebra `setPlugin` bug is a **"swap a pluggable component without re-running its initialization hook"** bug class: an admin swaps `plugin`, but the pool never re-derives `pluginConfig`/`fee` from the new plugin, so later (unprivileged) calls operate on stale, inconsistent state.

The closest local analog in `polkadot-sdk` is `pallet-contracts`'s `set_code_hash` host function, which lets a contract swap its own `code_hash` (the on-chain analog of a "plugin"/implementation) without ever invoking any initialization/constructor logic of the new code, leaving contract storage state — the analog of `pluginConfig` — stale and inconsistent with what the new code expects.

### Title
Contract code-hash swap via `set_code_hash` never re-initializes storage/config for the new code, leaving stale invariants exploitable post-upgrade - (`substrate/frame/contracts/src/exec.rs`)

### Summary
`Stack::set_code_hash` in `pallet-contracts` allows a running contract to change its own `code_hash` to any other uploaded code. It updates `info.code_hash`, refcounts, and the storage-deposit accounting, but it never invokes the new code's `deploy`/constructor entry point or otherwise re-establishes the storage invariants the new code expects. [1](#0-0) 

### Finding Description
`set_code_hash` mutates `frame.contract_info().code_hash` directly and only reconciles refcounts/deposit — it performs no call into the new code's initialization routine: [1](#0-0) 

This is exposed to contracts as a normal host function, callable from within any contract's own execution (the `call` entrypoint), i.e. reachable by ordinary, unprivileged transactions that invoke the contract: [2](#0-1) [3](#0-2) 

The API documentation itself only warns about address/determinism/call-stack revert caveats — it does not address the missing re-initialization of storage-derived configuration such as owner/role flags, accounting counters, or fee parameters that the new code's constructor would normally set: [4](#0-3) 

This is structurally identical to Algebra's `setPlugin`: an authorized action swaps the "implementation" (`plugin` ↔ `code_hash`) while the dependent local configuration (`pluginConfig`/`fee` ↔ contract storage the new code relies on for permissions/accounting) is left as set by the *previous* implementation. Any subsequent, fully unprivileged call into the contract then executes new code against old/foreign storage semantics.

By contrast, `pallet-revive` explicitly recognized this exact defect class for its immutable-data feature and disabled `set_code_hash` until a constructor re-run could be implemented, and it has since been removed entirely from `pallet-revive`: [5](#0-4) [6](#0-5) 

`pallet-contracts` (the original WASM contracts pallet, still shipped and used by chains that have not migrated to `pallet-revive`) never received an equivalent fix — `set_code_hash` remains fully active with no re-initialization hook.

### Impact Explanation
A contract implementing an upgrade pattern (proxy/self-upgrading contract) that calls `set_code_hash` leaves any storage slots the new code interprets as configuration (owner/admin flags, fee accounting, allowance/balance layout) exactly as the old code left them. If the new code assumes those slots start at their "constructor-initialized" defaults (e.g., "first caller after deploy becomes owner", or a fee/rate accumulator starts at zero), any unprivileged caller invoking the contract immediately after the code swap can trigger unauthorized execution, incorrect accounting, or fund misappropriation — the exact class of consequence (incorrect fee withdrawals, temporarily broken functionality) described in the Algebra report, materializing here as unauthorized state manipulation reachable by ordinary users, not the admin who performed the swap.

### Likelihood Explanation
Medium: it requires a contract to legitimately use `set_code_hash` in an upgrade path (a realistic and even encouraged pattern per the host function's own documentation), after which exploitation only needs an ordinary call from any account — no privileged, governance, or validator/collator access is required to trigger the resulting inconsistency.

### Recommendation
Either remove `set_code_hash` from `pallet-contracts` (as already done for `pallet-revive`, see `prdoc/stable2603/pr_10517.prdoc`), or require the new code's `deploy`/constructor entry point to run as part of `set_code_hash`, re-establishing the invariants the new code depends on before the swapped code becomes reachable, mirroring the recommendation given for Algebra's `setPlugin`.

### Proof of Concept
1. Deploy contract `A` whose `deploy()` constructor sets a storage flag `owner = caller` and whose `call()` checks `owner` before privileged actions.
2. Upload code `B` whose `call()` treats the same storage slot as a balance/allowance value rather than an owner address, and grants withdrawal rights to whoever satisfies a check against that slot's raw bytes.
3. From within `A`, call `api::set_code_hash(hash_of_B)` (as in the existing test fixture) — this only flips `code_hash`/refcounts, never running `B`'s own initialization: [7](#0-6) 
4. Any unprivileged account then calls the contract; `B`'s logic executes against the stale storage left by `A`, allowing unauthorized withdrawal/action without ever having satisfied `B`'s intended initialization checks, matching `set_code_extrinsic`/`set_code_hash` test scaffolding already present in the repo: [8](#0-7)

### Citations

**File:** substrate/frame/contracts/src/exec.rs (L1583-1611)
```rust
	fn set_code_hash(&mut self, hash: CodeHash<Self::T>) -> DispatchResult {
		let frame = top_frame_mut!(self);
		if !E::from_storage(hash, &mut frame.nested_gas)?.is_deterministic() {
			return Err(<Error<T>>::Indeterministic.into());
		}

		let info = frame.contract_info();

		let prev_hash = info.code_hash;
		info.code_hash = hash;

		let code_info = CodeInfoOf::<T>::get(hash).ok_or(Error::<T>::CodeNotFound)?;

		let old_base_deposit = info.storage_base_deposit();
		let new_base_deposit = info.update_base_deposit(&code_info);
		let deposit = StorageDeposit::Charge(new_base_deposit)
			.saturating_sub(&StorageDeposit::Charge(old_base_deposit));

		frame.nested_storage.charge_deposit(frame.account_id.clone(), deposit);

		Self::increment_refcount(hash)?;
		Self::decrement_refcount(prev_hash);
		Contracts::<Self::T>::deposit_event(Event::ContractCodeUpdated {
			contract: frame.account_id.clone(),
			new_code_hash: hash,
			old_code_hash: prev_hash,
		});
		Ok(())
	}
```

**File:** substrate/frame/contracts/src/wasm/runtime.rs (L2474-2488)
```rust
	/// See [`pallet_contracts_uapi::HostFn::set_code_hash`].
	#[prefixed_alias]
	#[mutating]
	fn set_code_hash(ctx: _, memory: _, code_hash_ptr: u32) -> Result<ReturnErrorCode, TrapReason> {
		ctx.charge_gas(RuntimeCosts::SetCodeHash)?;
		let code_hash: CodeHash<<E as Ext>::T> =
			ctx.read_sandbox_memory_as(memory, code_hash_ptr)?;
		match ctx.ext.set_code_hash(code_hash) {
			Err(err) => {
				let code = Runtime::<E>::err_into_return_code(err)?;
				Ok(code)
			},
			Ok(()) => Ok(ReturnErrorCode::Success),
		}
	}
```

**File:** substrate/frame/contracts/fixtures/contracts/set_code_hash.rs (L24-37)
```rust
#[no_mangle]
#[polkavm_derive::polkavm_export]
pub extern "C" fn deploy() {}

#[no_mangle]
#[polkavm_derive::polkavm_export]
pub extern "C" fn call() {
	input!(addr: [u8; 32],);
	api::set_code_hash(addr).unwrap();

	// we return 1 after setting new code_hash
	// next `call` will NOT return this value, because contract code has been changed
	api::return_value(uapi::ReturnFlags::empty(), &1u32.to_le_bytes());
}
```

**File:** substrate/frame/contracts/uapi/src/host.rs (L671-688)
```rust
	/// 2. Contracts using this API can't be assumed as having deterministic addresses. Said another
	/// way, when using this API you lose the guarantee that an address always identifies a specific
	/// code hash.
	///
	/// 3. If a contract calls into itself after changing its code the new call would use
	/// the new code. However, if the original caller panics after returning from the sub call it
	/// would revert the changes made by [`set_code_hash()`][`Self::set_code_hash`] and the next
	/// caller would use the old code.
	///
	/// # Parameters
	///
	/// - `code_hash`: The hash of the new code. Should be decodable as an `T::Hash`. Traps
	///   otherwise.
	///
	/// # Errors
	///
	/// - [CodeNotFound][`crate::ReturnErrorCode::CodeNotFound]
	fn set_code_hash(code_hash: &[u8]) -> Result;
```

**File:** prdoc/stable2412/pr_5861.prdoc (L26-29)
```text
      This PR also disables the `set_code_hash` API (which isn't usable for Solidity contracts
      without pre-compiles anyways). With immutable storage attached to contracts, we now want
      to run the constructor of the new code hash to collect the immutable data during
      `set_code_hash`. This will be implemented in a follow up PR.
```

**File:** prdoc/stable2603/pr_10517.prdoc (L1-9)
```text
title: '[pallet-revive] remove disabled host functions terminate and set_code_hash'
doc:
- audience: Runtime Dev
  description: |-
    fixes part of https://github.com/paritytech/polkadot-sdk/issues/8570

    Removes the following disabled host functions:
    - `terminate`
    - `set_code_hash`
```

**File:** substrate/frame/contracts/src/tests.rs (L3409-3443)
```rust
#[test]
fn set_code_hash() {
	let (wasm, code_hash) = compile_module::<Test>("set_code_hash").unwrap();
	let (new_wasm, new_code_hash) = compile_module::<Test>("new_set_code_hash_contract").unwrap();

	ExtBuilder::default().existential_deposit(100).build().execute_with(|| {
		let _ = <Test as Config>::Currency::set_balance(&ALICE, 1_000_000);

		// Instantiate the 'caller'
		let contract_addr = builder::bare_instantiate(Code::Upload(wasm))
			.value(300_000)
			.build_and_unwrap_account_id();
		// upload new code
		assert_ok!(Contracts::upload_code(
			RuntimeOrigin::signed(ALICE),
			new_wasm.clone(),
			None,
			Determinism::Enforced
		));

		System::reset_events();

		// First call sets new code_hash and returns 1
		let result = builder::bare_call(contract_addr.clone())
			.data(new_code_hash.as_ref().to_vec())
			.debug(DebugInfo::UnsafeDebug)
			.build_and_unwrap_result();
		assert_return_code!(result, 1);

		// Second calls new contract code that returns 2
		let result = builder::bare_call(contract_addr.clone())
			.debug(DebugInfo::UnsafeDebug)
			.build_and_unwrap_result();
		assert_return_code!(result, 2);

```
