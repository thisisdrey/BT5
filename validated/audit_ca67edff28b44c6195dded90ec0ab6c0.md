### Title
Value sent to stateless builtin pre-compile addresses is permanently locked with no withdrawal path - (File: `substrate/frame/revive/src/precompiles.rs`, `substrate/frame/revive/src/vm/pvm.rs`)

### Summary
Analogous to the Dahlia `payable` functions that silently accept `msg.value` with no way to ever move it back out, `pallet-revive`'s `call`/`call_evm` host functions unconditionally forward any non-zero `value` to the callee address before dispatching to a pre-compile, even when that pre-compile is a stateless, "no account, no state" builtin (`HAS_CONTRACT_INFO = false`, e.g. `ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, the `Bn128*` and `Blake2F` pre-compiles at fixed addresses `0x01`–`0x09`). These addresses have no owning key and no code path that ever spends or forwards a balance, so any value sent to them is stuck forever.

### Finding Description
`Precompile::HAS_CONTRACT_INFO` is documented to mean: "No account or any other state will be created for the address" when set to `false` [1](#0-0) . All of the built-in Ethereum-compatible pre-compiles (`ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128Add`, `Bn128Mul`, `Bn128Pairing`, `Blake2F`) are pure, stateless functions — none of their implementations ever call `transfer`, `terminate`, or otherwise route a received balance anywhere.

Despite this, the call-dispatch path in `pvm.rs::call` reads the `value` field and forwards it into `self.ext.call(resources, &callee, value, input_data, ...)` regardless of whether the target is a precompile, contract, or plain account [2](#0-1) . The only condition checked is `read_only`/`is_read_only`, not whether the destination is a stateless pre-compile that has no mechanism to use or return the value. The same unconditional value-forwarding also happens in the legacy `wasm/runtime.rs::call` path [3](#0-2) .

This is confirmed empirically by the test `pure_precompile_works`, which calls each fixed-address pre-compile (`H160::from_low_u64_be(1)` through `9`) with `native_value(1_000)` on the caller and `100u64` forwarded per call, and then asserts the balance actually accrues at the pre-compile address: [4](#0-3) 

Because `ECRecover`/`Sha256`/etc. addresses are fixed, well-known, non-owned system addresses with no private key and no contract code that can be deployed there (the matcher reserves them), there is no way — by design — for anyone to later withdraw that balance. It is functionally identical to Dahlia's `payable withdraw`/`claimInterest`: an entry point that legitimately accepts a value transfer but has no logic to use or refund it, and no other code path exists to reclaim it.

### Impact Explanation
Any unprivileged user (or a contract acting on a user's behalf) who sends native value to one of the fixed built-in pre-compile addresses — which is trivial to do accidentally, since these addresses look like ordinary EVM addresses and are exactly the addresses Solidity code uses for `staticcall(gas, 0x01, ...)`-style calls when composing value with a call — permanently and irrecoverably loses those funds. This satisfies the "permanent user-fund lock" impact category from the gate: no admin, governance, validator, or malicious-actor involvement is required; it is a straightforward consequence of the public call/value-forwarding path not distinguishing stateless pre-compiles from real accounts or contracts.

### Likelihood Explanation
High likelihood of accidental triggering: any Solidity-style low-level call with `value` attached to addresses `0x01`–`0x09` (which are the standard, widely-known Ethereum pre-compile addresses that developers copy from other EVM chains) results in a stuck-fund transfer. No privileged access or special conditions are needed — a single `call{value: v}(precompileAddr, ...)` from any account suffices.

### Recommendation
Reject non-zero `value` transfers to any pre-compile address where `HAS_CONTRACT_INFO == false` (or more generally, to any pre-compile that has no mechanism to spend/forward balance) before performing the transfer, returning `TransferFailed`/`StateChangeDenied` instead of silently crediting the address. Alternatively, route such value through the normal existential/refund mechanism so unused value is returned to the caller rather than deposited into an address that can never spend it.

### Proof of Concept
1. Fund `ALICE` and deploy a simple caller contract as in `pure_precompile_works`.
2. From the caller contract, invoke `api::call` (or `call_evm`) targeting `H160::from_low_u64_be(1)` (the `ECRecover` pre-compile) with a non-zero `value`, e.g. `100`.
3. Observe (as the existing test already asserts) that `Pallet::<Test>::evm_balance(&precompile_addr)` becomes `100` [5](#0-4) .
4. There is no extrinsic, host function, or governance call anywhere in `pallet-revive` that can withdraw balance from address `0x0000...0001` (no private key exists for it, and the pre-compile's `call` implementation only computes ECRecover output — it never touches its own balance). The `100` units are permanently locked.

### Citations

**File:** substrate/frame/revive/src/precompiles.rs (L190-193)
```rust
	/// # When set to **false**
	///
	/// - No account or any other state will be created for the address.
	/// - Only `call` should be implemented. `call_with_info` is never called.
```

**File:** substrate/frame/revive/src/vm/pvm.rs (L673-696)
```rust
		let call_outcome = match call_type {
			CallType::Call { value_ptr } => {
				let read_only = flags.contains(CallFlags::READ_ONLY);
				let value = memory.read_u256(value_ptr)?;
				if value > 0u32.into() {
					// If the call value is non-zero and state change is not allowed, issue an
					// error.
					if read_only || self.ext.is_read_only() {
						return Err(Error::<E::T>::StateChangeDenied.into());
					}

					self.charge_gas(RuntimeCosts::CallTransferSurcharge {
						dust_transfer: Pallet::<E::T>::has_dust(value),
					})?;
				}

				let reentrancy = if flags.contains(CallFlags::ALLOW_REENTRY) {
					ReentrancyProtection::AllowReentry
				} else {
					ReentrancyProtection::Strict
				};

				self.ext.call(resources, &callee, value, input_data, reentrancy, read_only)
			},
```

**File:** substrate/frame/contracts/src/wasm/runtime.rs (L1006-1035)
```rust
		let call_outcome = match call_type {
			CallType::Call { callee_ptr, value_ptr, deposit_ptr, weight } => {
				let callee: <<E as Ext>::T as frame_system::Config>::AccountId =
					self.read_sandbox_memory_as(memory, callee_ptr)?;
				let deposit_limit: BalanceOf<<E as Ext>::T> = if deposit_ptr == SENTINEL {
					BalanceOf::<<E as Ext>::T>::zero()
				} else {
					self.read_sandbox_memory_as(memory, deposit_ptr)?
				};
				let read_only = flags.contains(CallFlags::READ_ONLY);
				let value: BalanceOf<<E as Ext>::T> =
					self.read_sandbox_memory_as(memory, value_ptr)?;
				if value > 0u32.into() {
					// If the call value is non-zero and state change is not allowed, issue an
					// error.
					if read_only || self.ext.is_read_only() {
						return Err(Error::<E::T>::StateChangeDenied.into());
					}
					self.charge_gas(RuntimeCosts::CallTransferSurcharge)?;
				}
				self.ext.call(
					weight,
					deposit_limit,
					callee,
					value,
					input_data,
					flags.contains(CallFlags::ALLOW_REENTRY),
					read_only,
				)
			},
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L4751-4773)
```rust
	for (description, precompile_addr, input, output) in cases {
		let (code, _code_hash) = compile_module("call_and_return").unwrap();
		ExtBuilder::default().build().execute_with(|| {
			let _ = <Test as Config>::Currency::set_balance(&ALICE, 100_000_000_000);
			let Contract { addr, .. } = builder::bare_instantiate(Code::Upload(code))
				.native_value(1_000)
				.build_and_unwrap_contract();

			let result = builder::bare_call(addr)
				.data(
					(&precompile_addr, 100u64)
						.encode()
						.into_iter()
						.chain(input)
						.collect::<Vec<_>>(),
				)
				.build_and_unwrap_result();

			assert_eq!(
				Pallet::<Test>::evm_balance(&precompile_addr),
				U256::from(100),
				"{description}: unexpected balance"
			);
```
