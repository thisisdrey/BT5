## Analysis

The external report's core invariant is: *a public entrypoint accepts value transfers it has no logic to move back out, so accidentally-sent funds become permanently stuck.* The direct analog in `pallet-revive` is the built-in precompile call path: `Pallet::call`/EVM `CALL` unconditionally moves native value to any callee address — including the fixed, code-less built-in precompiles (`ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128*`, `Blake2F`) — before the pallet even checks what kind of precompile it is, and these precompiles have no logic whatsoever to relay or return that value.

### Title
Value sent to stateless built-in precompiles (ECRecover, Sha256, Identity, Modexp, Bn128*, Blake2F) is permanently locked - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`Stack::run` transfers the caller-supplied `value` to the destination account unconditionally, before determining whether the destination is a real contract, an EOA, or a built-in precompile. For built-in precompiles with `HAS_CONTRACT_INFO = false` (e.g. `ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, the `Bn128` family, `Blake2F`), the precompile logic only performs pure cryptographic computation and never forwards, refunds, or otherwise makes use of any transferred value. Since these are fixed, deterministic addresses with no controlling private key and no contract code, any native value routed to them via `call{value: ...}` becomes permanently unspendable.

### Finding Description
In `new_frame`/`run` (`substrate/frame/revive/src/exec.rs`), every non-delegate call transfers `frame.value_transferred` to the destination account before dispatching to the precompile or contract executable: [1](#0-0) 

Only precompiles with `HAS_CONTRACT_INFO = true` get special account bookkeeping (minted ED, consumer reference) to represent "real" state: [2](#0-1) 

But the balance transfer itself happens regardless of whether the destination is a stateless precompile. The stateless built-in precompiles (`HAS_CONTRACT_INFO: bool = false`) such as `ECRecover`, `Sha256`, `Identity`, `Modexp`, `Bn128*`, and `Blake2F` only implement the `call` entrypoint to perform a pure cryptographic computation and return output bytes — none of them read, forward, refund, or otherwise account for the `value` sent alongside the call: [3](#0-2) 

The codebase's own test suite confirms the transferred value is retained at the precompile's address after the call completes, proving the funds land there and stay there: [4](#0-3) 

Tellingly, the team was aware of exactly this risk for at least one precompile (`PointEval`, address `0x0a`, not yet implemented) and deliberately chose to reject the call outright rather than silently accept and strand the value: [5](#0-4) 

That mitigation — "fails the call instead of doing a silent balance transfer" — was not applied to the already-implemented and shipped stateless precompiles (`ECRecover`, `Sha256`, `Ripemd160`, `Identity`, `Modexp`, `Bn128Add/Mul/Pairing`, `Blake2F`), which are the ones actually reachable by users with real value at stake.

### Impact Explanation
Any signed account or contract that sends native value (or an EVM-encoded value) to one of these fixed addresses — accidentally, via a fat-fingered address, a buggy client, or a contract that blindly forwards `msg.value` to a computed address that happens to collide with a low, reserved address (`0x1`–`0x9`) — permanently loses that value. There is no key holder for these addresses, no contract code to invoke a withdrawal, and no governance/sweep mechanism identified in the pallet for reclaiming balances stuck at precompile addresses. This is a direct instance of "permanent user-fund lock," an explicitly in-scope impact category.

### Likelihood Explanation
The precompile addresses (`0x1`–`0x9`) are exactly the same low, well-known addresses used across all EVM-compatible chains, so they are highly likely to be targeted unintentionally (copy-pasted contract logic, address-derivation collisions, or simple mistakes when composing calldata for `call{value}`). No privileged actor, governance action, or malicious peer/validator is required — any ordinary unprivileged account triggers the loss purely through normal use of the public `call`/EVM `CALL` opcode.

### Recommendation
Apply the same guard used for `PointEval` uniformly: reject calls carrying non-zero `value` to stateless built-in precompiles (`HAS_CONTRACT_INFO = false`) that have no mechanism to account for or relay that value, instead of silently transferring and stranding it. Alternatively, move the `value` transfer step in `Stack::run`/`new_frame` to occur only after confirming the destination can meaningfully hold or use it (i.e., is a real contract, EOA, or precompile explicitly documented to accept value).

### Proof of Concept
1. Fund an account/contract with native balance.
2. Issue an EVM `CALL` (or `pallet-revive`'s `call` extrinsic) to address `0x0000...0004` (`Identity` precompile) with a non-zero `value` and arbitrary input data.
3. Observe (as validated by the existing test `pure_precompile_works`/`pure_precompile_works`-style assertions in `substrate/frame/revive/src/tests/pvm.rs`) that `Pallet::<Test>::evm_balance(&precompile_addr)` reflects the transferred amount after the call.
4. There is no subsequent call, extrinsic, or governance action in the codebase capable of moving that balance out of the precompile's address — the funds are permanently locked.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1375-1387)
```rust
			// Every non delegate call or instantiate also optionally transfers the balance.
			// If it is a delegate call, then we've already transferred tokens in the
			// last non-delegate frame.
			if frame.delegate.is_none() {
				Self::transfer_from_origin(
					&self.origin,
					&caller,
					account_id,
					frame.value_transferred,
					&mut frame.frame_meter,
					self.exec_config,
				)?;
			}
```

**File:** substrate/frame/revive/src/exec.rs (L1389-1405)
```rust
			// We need to make sure that the pre-compiles contract exist before executing it.
			// A few more conditionals:
			// 	- Only contracts with extended API (has_contract_info) are guaranteed to have an
			//    account.
			//  - Only when not delegate calling we are executing in the context of the pre-compile.
			//    Pre-compiles itself cannot delegate call.
			if let Some(precompile) = executable.as_precompile() &&
				precompile.has_contract_info() &&
				frame.delegate.is_none() &&
				!<System<T>>::account_exists(account_id)
			{
				// prefix matching pre-compiles cannot have a contract info
				// hence we only mint once per pre-compile
				T::Currency::mint_into(account_id, T::Currency::minimum_balance())?;
				// make sure the pre-compile does not destroy its account by accident
				<System<T>>::inc_consumers(account_id)?;
			}
```

**File:** substrate/frame/revive/src/precompiles.rs (L216-224)
```rust
	/// Entry point for your pre-compile when `HAS_CONTRACT_INFO = false`.
	#[allow(unused_variables)]
	fn call(
		address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		unimplemented!("{UNIMPLEMENTED}")
	}
```

**File:** substrate/frame/revive/src/tests/pvm.rs (L4751-4774)
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
			assert_eq!(
```

**File:** substrate/frame/revive/src/precompiles/builtin/point_eval.rs (L33-41)
```rust
	fn call(
		_address: &[u8; 20],
		_input: Vec<u8>,
		_env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		// Exists on Ethereum but we didn't implement it, yet.
		// This fails the call instead of doing a silent balance transfer.
		Err(<CrateError<T>>::UnsupportedPrecompileAddress.into())
	}
```
