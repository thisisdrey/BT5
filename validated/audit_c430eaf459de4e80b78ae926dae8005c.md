This confirms the empirical evidence in the claim: the test `pure_precompile_works` at `substrate/frame/revive/src/tests/pvm.rs` lines 4751-4781 sends `native_value(1_000)` with `value=100` to fixed-address precompiles (ECRecover, Sha256, etc., all `HAS_CONTRACT_INFO = false`), and asserts `Pallet::<Test>::evm_balance(&precompile_addr) == U256::from(100)` after the call succeeds — confirming value transfer happens unconditionally regardless of precompile status. [1](#0-0) 

The `Stack::run` function in `exec.rs` confirms the transfer happens unconditionally for non-delegate calls via `Self::transfer_from_origin(...)`, before the precompile execution, and the account-creation/consumer-protection logic (`mint_into`/`inc_consumers`) is gated strictly behind `precompile.has_contract_info()`. [2](#0-1) 

The `Precompile` trait documentation in `precompiles.rs` explicitly states that when `HAS_CONTRACT_INFO` is `false`, "No account or any other state will be created for the address," confirming the design intent that these addresses aren't supposed to hold any persistent state, including balance. [3](#0-2) 

I confirmed the `ERC20` precompile at `substrate/frame/assets/precompiles/src/lib.rs` sets `HAS_CONTRACT_INFO = false` and its `call()` dispatches to helper functions without any handling of `env.value_transferred()`, and the `XcmPrecompile` at `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs` similarly ignores `value_transferred` in its `call()` implementation. These all match the citations already provided in the claim.

All the cited code paths, tests, and trait definitions in the claim are verified accurate against the current repository state. The exploit path is real: an unprivileged caller can attach non-zero `value` to any call targeting a `HAS_CONTRACT_INFO = false` precompile address (ECRecover, Sha256, ERC20 precompile instances, XcmPrecompile, etc.), the value gets unconditionally transferred to the precompile's mapped account via `transfer_from_origin`/`transfer`, and there is no dispatchable or precompile function that can move funds back out of that address since it's not an EOA and has no contract logic managing an outgoing transfer path. This constitutes a permanent user-fund lock, which is a valid impact category under the Polkadot SDK Impact Gate.

Audit Report

## Title
Native value sent to `HAS_CONTRACT_INFO = false` precompiles (ERC20/XCM) is silently accepted and permanently locked - (File: `substrate/frame/revive/src/exec.rs`)

## Summary
`pallet-revive`'s call dispatch transfers attached native `value` to the callee's mapped account before invoking the callee's logic, with no distinction for precompiles. Several built-in precompiles (`ERC20` in `substrate/frame/assets/precompiles/src/lib.rs`, `XcmPrecompile` in `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs`) declare `HAS_CONTRACT_INFO = false`, meaning "no account or any other state will be created for the address" per their own documentation, yet the value-transfer path runs regardless and does not check whether the target function is meant to receive value.

## Finding Description
In `Stack::run`'s inner `do_transaction` closure, the balance transfer from origin to the destination account happens unconditionally for any non-delegate call via `Self::transfer_from_origin(...)` [4](#0-3) . This runs before the precompile's `call()` handler is invoked, and runs whether or not the target is a precompile. Immediately after, the code only special-cases account creation/consumer-protection for precompiles with `has_contract_info()` [5](#0-4) .

For precompiles with `HAS_CONTRACT_INFO = false`, the trait documentation states the design intent is that "No account or any other state will be created for the address" [3](#0-2) . This is confirmed empirically by the `pure_precompile_works` test, where sending `native_value(1_000)`/`value=100` to fixed-address precompiles like ECRecover results in `Pallet::<Test>::evm_balance(&precompile_addr) == U256::from(100)` [1](#0-0) .

None of the affected precompiles' `call()` implementations read `env.value_transferred()`, forward it, or reject it. Because the precompile's mapped account never gets `inc_consumers`/`mint_into` treatment (reserved for `has_contract_info()` precompiles), and because no code path exists that lets the precompile logic move funds out of its own mapped account, any native value attached to a call into these addresses is credited to an address with no spending authority.

## Impact Explanation
This falls under "permanent user-fund lock." A user or dApp/wallet that attaches non-zero value alongside a call to a `HAS_CONTRACT_INFO = false` precompile (fixed-address system precompiles like ECRecover/Sha256, the ERC20 asset precompile, or the fixed-address XCM precompile) will have that value irrecoverably stuck at an address with no spending authority — these addresses are not EOAs, and the precompiles have no mechanism to reclaim or forward the balance.

## Likelihood Explanation
Any unprivileged caller can trigger this by simply attaching a non-zero `value` to a call targeting these precompile addresses — no special privileges, malicious peers, governance, or off-chain assumptions are required. The precompile addresses are well-known/fixed (e.g., `H160::from_low_u64_be(1)` for ECRecover, address `10` for `XcmPrecompile`), making accidental fund loss plausible from tooling that doesn't omit `msg.value` by default.

## Recommendation
For `HAS_CONTRACT_INFO = false` precompiles that do not implement value handling, reject any call carrying non-zero `value_transferred()` before the generic transfer executes, i.e., check `env.value_transferred().is_zero()` at the top of `call()`, or move the value check earlier in `Stack::run`/`do_transaction` so that a call into a precompile without a defined value-handling contract fails fast instead of silently locking funds.

## Proof of Concept
1. Deploy or use a fixed-address precompile with `HAS_CONTRACT_INFO = false` (e.g., ECRecover at `H160::from_low_u64_be(1)`, or the ERC20/XcmPrecompile).
2. From an unprivileged EOA/contract, invoke a call targeting the precompile address with a non-zero `value`.
3. Observe the call succeeds (precompile logic executes normally, ignoring `value`), and per `pure_precompile_works`, `Pallet::<T>::evm_balance(&precompile_addr)` now reflects the transferred amount.
4. Confirm there is no dispatchable, precompile function, or mechanism that lets this balance be moved out of the precompile's mapped account — the funds are permanently stranded.

### Citations

**File:** substrate/frame/revive/src/tests/pvm.rs (L4751-4781)
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
				alloy_core::hex::encode(result.data),
				alloy_core::hex::encode(output),
				"{description} Unexpected output for precompile: {precompile_addr:?}",
			);
			assert_eq!(result.flags, ReturnFlags::empty());
		});
	}
```

**File:** substrate/frame/revive/src/exec.rs (L1375-1405)
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

**File:** substrate/frame/revive/src/precompiles.rs (L190-193)
```rust
	/// # When set to **false**
	///
	/// - No account or any other state will be created for the address.
	/// - Only `call` should be implemented. `call_with_info` is never called.
```
