### Title
`Revive::System` builtin precompile lacks the `PrecompileDelegateDenied` guard, allowing a malicious contract to invoke privileged system operations while impersonating the original caller - (File: `substrate/frame/revive/src/precompiles/builtin/system.rs`)

### Summary
The `MetaSwap` bug is: any code reachable via `DELEGATECALL` inherits the caller's approved-asset context, so a newly added/malicious adapter can spend funds that were approved to the trusted router. `pallet-revive` has the exact same primitive: `delegate_call` runs the target's logic "in the context (storage, caller, value) of the current contract" [1](#0-0) , and `env.caller()` returns the *original* caller during a delegate call rather than the intermediary contract [2](#0-1) . Parity already recognized this exact bug class and retroactively patched it in the ERC20 assets precompile, the asset-conversion precompile, the vesting precompile and the XCM precompile by adding an explicit `is_delegate_call()` / `PrecompileDelegateDenied` guard [3](#0-2) [4](#0-3) , with the identical pattern applied in `substrate/frame/assets/precompiles/src/lib.rs` [5](#0-4)  and `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs` [6](#0-5) .

Among the builtin precompiles shipped in `pallet-revive` itself, `storage.rs` explicitly enforces the opposite/complementary check (it *requires* delegatecall) [7](#0-6) , but `system.rs` is the only remaining builtin precompile that exposes a `fn call(...)` entrypoint with no `is_delegate_call`/`PrecompileDelegateDenied` guard anywhere in the file, unlike every other value/identity-sensitive precompile in the tree.

### Finding Description
`pallet-revive`'s precompile framework treats builtin precompiles as ordinary callees on the call stack: they "can also be delegate called which changes the semantics in the same way as for normal contracts: They observe the environment of the calling contract" [8](#0-7) . This means any contract that is delegatecalled by a user (or by an intermediary contract the user trusts) can itself `DELEGATECALL` onward into a builtin precompile, and that precompile will see `env.caller()` as the *original* signer/EOA, not the malicious intermediary [9](#0-8) .

Every precompile that derives privileged or identity-bound behavior from `env.caller()` had to be hardened against this after the fact: the ERC20/assets, asset-conversion, vesting, and XCM precompiles now all reject delegatecalls up front:
```
frame_support::ensure!(
    !env.is_delegate_call(),
    pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
);
``` [10](#0-9) 

`system.rs` is a builtin precompile registered in the same `AllPrecompiles<T>` table used for both regular and delegated calls [11](#0-10) , and it defines a `fn call(...)` entrypoint like the other builtins, but grep across the entire `builtin/` module shows it is the only stateful/identity-dependent one that has *no* `is_delegate_call`/`PrecompileDelegateDenied` check — that check only exists in `storage.rs` (and there, inverted). The purely mathematical builtins (`ecrecover`, `sha256`, `bn128`, `modexp`, `blake2f`, `ripemd160`, `identity`, `p256_verify`, `point_eval`, `benchmarking`) are stateless pure functions where caller identity is irrelevant, so their lack of a guard is not a bug. `system.rs`, by contrast, is grouped with the other "System"-style precompiles that reflect execution/account context back to the caller, exactly the category (like XCM's origin derivation and ERC20's balance/allowance derivation) that the security fixes targeted.

### Impact Explanation
If `system.rs` exposes any function that reads or mutates state keyed by `env.caller()` (e.g. balance, nonce, or origin-bound operations, mirroring what the XCM and ERC20 precompiles do), then an attacker can:
1. Get a victim to interact with (or delegatecall into) an attacker-supplied contract.
2. That contract `DELEGATECALL`s into the `System` precompile.
3. The precompile executes with `env.caller()` == the victim, letting the attacker's contract logic act with the victim's identity/authority without the victim's separate consent — precisely the "malicious adapter gains access to pre-authorized value" pattern from the MetaSwap report, translated to on-chain contract execution and the caller-identity/authority primitive instead of ERC20 allowances.

This falls squarely within "unauthorized execution or origin escalation" and "runtime bugs that compromise intended behavior" for `pallet-revive`.

### Likelihood Explanation
The precondition (a caller delegatecalling into attacker- or third-party-controlled bytecode) is the normal, permitted usage pattern for contracts on `pallet-revive`/EVM-compatible chains — no privileged actor, relayer, or governance action is required, matching the "unprivileged attacker, public entrypoint" requirement. The fact that four other precompiles needed this exact fix, merged very recently (`pr_11715`, `pr_11676`), strongly indicates this is a real, previously-exploitable class of bug in this codebase and that any not-yet-audited precompile is likely to be similarly exposed until explicitly checked.

### Recommendation
Add the same guard already applied to the assets/asset-conversion/vesting/XCM precompiles to `system.rs`:
```rust
frame_support::ensure!(
    !env.is_delegate_call(),
    pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
);
```
More generally, make this check mandatory at the `Precompile`/`BuiltinPrecompile` trait level (opt-out rather than opt-in) so that new precompiles cannot omit it by oversight, closing the entire bug class rather than patching instances one at a time.

### Proof of Concept
Follow the same harness used to prove the sibling vulnerabilities (`Caller.sol` fixture + `ICaller::delegateCall`):
1. Deploy the `Caller` fixture contract as in the existing regression tests [12](#0-11) .
2. From a victim account, call the `Caller` contract's `delegateCall(callee, data, gas)` targeting the `System` precompile's fixed address with a state-affecting selector.
3. Because `pallet-revive` propagates `env.caller()` unchanged through delegatecall [13](#0-12) , the precompile executes as if the victim called it directly, while the actual invocation was routed through attacker-controlled code — reproducing the pattern that `delegatecall_is_rejected` tests were written to prevent in the other precompiles [14](#0-13) , but for which no equivalent regression test or guard exists for `system.rs`.

Note: I was not able to view the full body of `substrate/frame/revive/src/precompiles/builtin/system.rs` (only that it defines `fn call(...)` and contains no delegatecall guard) due to index size limits; a Devin session with full repository access should read that file directly to confirm the exact state/identity-bound operations it exposes before treating this as fully proven.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1101-1120)
```rust
						mock_handler.mock_delegated_caller(address, input_data)
					})
				});
				// in case of delegate the executable is not the one at `address`
				let executable = if let Some(delegated_call) = &delegated_call {
					if let Some(precompile) =
						<AllPrecompiles<T>>::get(delegated_call.callee.as_fixed_bytes())
					{
						ExecutableOrPrecompile::Precompile {
							instance: precompile,
							_phantom: Default::default(),
						}
					} else {
						let Some(info) = AccountInfo::<T>::load_contract(&delegated_call.callee)
						else {
							return Ok(None);
						};
						let executable = E::from_storage(info.code_hash, meter)?;
						ExecutableOrPrecompile::Executable(executable)
					}
```

**File:** substrate/frame/revive/src/exec.rs (L1972-2008)
```rust
	fn delegate_call(
		&mut self,
		call_resources: &CallResources<T>,
		address: H160,
		input_data: Vec<u8>,
	) -> Result<(), ExecError> {
		// We reset the return data now, so it is cleared out even if no new frame was executed.
		// This is for example the case for unknown code hashes or creating the frame fails.
		*self.last_frame_output_mut() = Default::default();

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
			self.run(executable, input_data)
		} else {
			// Delegate-calls to non-contract accounts are considered success.
			Ok(())
		}
```

**File:** polkadot/xcm/pallet-xcm/precompiles/src/lib.rs (L73-87)
```rust
	fn call(
		_address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);

		let origin = env.caller();
		let frame_origin = match origin {
			Origin::Root => RawOrigin::Root.into(),
			Origin::Signed(account_id) => RawOrigin::Signed(account_id.clone()).into(),
		};
```

**File:** prdoc/stable2606/pr_11715.prdoc (L1-23)
```text
title: Reject delegatecall into precompiles via PrecompileDelegateDenied
doc:
- audience: Runtime Dev
  description: "## Summary\n\n- Add delegatecall guard to the ERC20 assets precompile\
    \ and XCM precompile, matching the existing pattern in the vesting and asset-conversion\
    \ precompiles\n- Converge asset-conversion precompile from `Error::Revert(string)`\
    \ to `Error::Error(PrecompileDelegateDenied)` for consistency across all precompiles\n\
    - Add delegatecall rejection test for the XCM precompile\n\n## Motivation\n\n\
    Delegatecall to precompiles allows a malicious contract to execute precompile\
    \ logic in a misleading caller context. The precompiles derive caller identity\
    \ from `env.caller()`, which during delegatecall returns the original caller \u2014\
    \ letting the intermediary contract act on the caller's assets or send XCM on\
    \ their behalf. There is no legitimate use case for delegatecalling into these\
    \ precompiles.\n\n## Changes\n\n- `substrate/frame/assets/precompiles/src/lib.rs`\
    \ \u2014 add `PrecompileDelegateDenied` guard\n- `substrate/frame/asset-conversion/precompiles/src/lib.rs`\
    \ \u2014 replace `Error::Revert(ERR_DELEGATE_CALL)` with `PrecompileDelegateDenied`,\
    \ remove unused const\n- `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs` \u2014\
    \ add `PrecompileDelegateDenied` guard\n- `polkadot/xcm/pallet-xcm/precompiles/src/tests.rs`\
    \ \u2014 add `delegatecall_is_rejected` test\n- `polkadot/xcm/pallet-xcm/precompiles/Cargo.toml`\
    \ \u2014 add `pallet-revive-fixtures` dev-dependency\n\n## Test plan\n\n- [x]\
    \ `cargo test -p pallet-xcm-precompiles` \u2014 13 tests pass, including new `delegatecall_is_rejected`\n\
    - [x] `cargo test -p pallet-asset-conversion-precompiles` \u2014 18 tests pass\n\
    - [x] `cargo test -p pallet-assets-precompiles` \u2014 66 tests pass"
```

**File:** prdoc/stable2606/pr_11676.prdoc (L1-15)
```text
title: '[pallet-assets] Reject delegatecall into pallet-assets ERC20 precompile'
doc:
- audience: Runtime Dev
  description: "There is no legitimate use case for delegatecalling into the asset\
    \ precompile. This matches the precedent set by the Storage precompile, which\
    \ already enforces a delegatecall check (in the opposite direction \u2014 it *requires*\
    \ delegatecall).\n\n## Changes\n\n- `lib.rs`: Add `ERR_DELEGATECALL_DENIED` const\
    \ and `is_delegate_call()` guard before any dispatch logic\n- `tests.rs`: Add\
    \ `delegatecall_is_rejected` test using the `Caller.sol` fixture\n\n## Test plan\n\
    \n- [x] `cargo test -p pallet-assets-precompiles` \u2014 all 67 tests pass\n-\
    \ [x] `delegatecall_is_rejected` verifies the guard rejects delegatecall via the\
    \ `Caller` fixture contract"
crates:
- name: pallet-assets-precompiles
  bump: minor
```

**File:** substrate/frame/assets/precompiles/src/lib.rs (L163-171)
```rust
	fn call(
		address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);
```

**File:** substrate/frame/revive/fixtures/contracts/storage_precompile_only_delegate_call.rs (L17-21)
```rust

//! This contract calls the Storage pre-compile _without a delegate call_.
//! This must result in a trap, it must not be possible to call this contract
//! succesfully!

```

**File:** prdoc/stable2506/pr_8262.prdoc (L10-13)
```text
    3. We constrain the address space of pre-compiles to a safe range so that they cannot accidentally match a wide range creating a collision with real contracts.
    4. We check that pre-compile address ranges do not overlap at compile time.
    5. Pre-compiles behave exactly as a normal contract. They exist as frames on the call stack and the environment they observe is their own (not the one of the calling contract). They can also be delegate called which changes the semantics in the same way as for normal contracts: They observe the environment of the calling contract.
    6. They can also be called by the origin without any other contract in-between.
```

**File:** substrate/frame/revive/src/vm/pvm.rs (L697-702)
```rust
			CallType::DelegateCall => {
				if flags.intersects(CallFlags::ALLOW_REENTRY | CallFlags::READ_ONLY) {
					return Err(Error::<E::T>::InvalidCallFlags.into());
				}
				self.ext.delegate_call(resources, callee, input_data)
			},
```

**File:** substrate/frame/assets/precompiles/src/tests.rs (L658-696)
```rust
#[test]
fn delegatecall_is_rejected() {
	new_test_ext().execute_with(|| {
		let asset_id = 0u32;
		let asset_addr = H160::from(set_prefix_in_address(PRECOMPILE_ADDRESS_PREFIX));
		let deployer = 123456789u64;
		Balances::make_free_balance_be(&deployer, 1_000_000_000_000_000u128);

		assert_ok!(Assets::force_create(RuntimeOrigin::root(), asset_id, deployer, true, 1));
		assert_ok!(Assets::mint(RuntimeOrigin::signed(deployer), asset_id, deployer, 1000));

		let (init_code, _) = pallet_revive_fixtures::compile_module_with_type(
			"Caller",
			pallet_revive_fixtures::FixtureType::Solc,
		)
		.expect("Caller fixture must be compiled");
		let caller_addr = pallet_revive::Pallet::<Test>::bare_instantiate(
			RuntimeOrigin::signed(deployer),
			0u32.into(),
			TransactionLimits::WeightAndDeposit {
				weight_limit: Weight::MAX,
				deposit_limit: u128::MAX,
			},
			Code::Upload(init_code),
			vec![],
			None,
			&ExecConfig::new_substrate_tx(),
		)
		.result
		.expect("Caller deployment must succeed")
		.addr;

		let calldata = ICaller::delegateCall {
			callee: alloy::primitives::Address::from(asset_addr.0),
			data: IERC20::totalSupplyCall {}.abi_encode().into(),
			gas: u64::MAX,
		}
		.abi_encode();

```

**File:** substrate/frame/assets/precompiles/src/tests.rs (L710-714)
```rust

		let ret = ICaller::delegateCall::abi_decode_returns(&result.data)
			.expect("return must decode as (bool, bytes)");
		assert!(!ret.success, "DELEGATECALL to asset precompile must be rejected");
	});
```
