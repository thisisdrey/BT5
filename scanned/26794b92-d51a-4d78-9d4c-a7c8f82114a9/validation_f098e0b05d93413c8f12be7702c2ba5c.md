## Title
System precompile (`terminate`/`callerIsRoot`/`callerIsOrigin`) is not delegatecall-denied, allowing a malicious library contract to self-destruct or impersonate the delegating contract — (File: `substrate/frame/revive/src/precompiles/builtin/system.rs`)

## Summary
The Weiroll report's core broken invariant is: *a contract reachable only through `delegatecall` fails to check that it is actually being executed in a trusted, expected context, so an attacker can drive privileged/state-destroying operations (self-destruct) through the delegatecall path.* `pallet-revive` already recognizes this exact class of bug and has patched it for several precompiles (`Storage`, ERC20 `Assets`, `AssetConversion`, `Vesting`, `XCM`) by adding an `is_delegate_call()` guard / `PrecompileDelegateDenied` check, with the documented rationale that "delegatecall to precompiles allows a malicious contract to execute precompile logic in a misleading caller context" [1](#0-0) . The `System` builtin precompile, however, was not included in that hardening pass and still lacks any delegatecall guard [2](#0-1) .

## Finding Description
`System::call` dispatches `terminate`, `callerIsRoot`, `callerIsOrigin`, and `ownCodeHash` purely based on decoded input, with no check on `env.is_delegate_call()` [3](#0-2) . By contrast, every other precompile that exposes caller-identity-sensitive or state-mutating operations now explicitly rejects delegatecall:
- `Storage` requires delegatecall (opposite direction, but still gated) [4](#0-3) 
- `Assets` ERC20, `AssetConversion`, `Vesting`, and `XCM` precompiles reject delegatecall specifically because `env.caller()` "during delegatecall returns the original caller — letting the intermediary contract act on the caller's assets or send XCM on their behalf" [5](#0-4) , and this is implemented as a guard before any dispatch logic in the XCM precompile [6](#0-5) .

`System::terminate` calls `env.terminate_caller(&h160)` [7](#0-6) . Because delegatecall preserves the caller's account/storage context — a delegate-called contract runs with the delegator's `account_id`, `contract_info`, and value, only substituting the executed code and recording `DelegateInfo{ caller, callee }` [8](#0-7)  — any contract `A` that delegatecalls into an untrusted/compromised library `L` (a normal, legitimate pattern for shared logic/gas savings, exactly like Weiroll's `delegatecall`-based library-swap wallet) executes `L`'s code with `A`'s account identity still on the frame stack. If `L` then makes a **regular call** into the `System` precompile's `terminate` function, `terminate_caller` operates on the frame belonging to `A` (the delegator), because the call-context/caller resolution logic (`Stack::caller`) walks the frame stack and, absent an override, resolves the caller identity from the frame beneath the callee — which is `A`'s frame, carrying `A`'s `account_id` [9](#0-8) . The same identity-confusion issue applies to `callerIsRoot`/`callerIsOrigin`/`ownCodeHash`, which is precisely the misleading-caller-context bug that PR 11715 fixed for the other precompiles [10](#0-9) .

This is a direct structural analog to the Weiroll finding: an implementation/library contract reached via `delegatecall` is able to trigger a destructive operation (self-destruct / `terminate`) or spoof identity checks (`callerIsRoot`) against the calling contract's own account, because the precompile does not verify it is being invoked from a trusted, non-delegatecall context — the exact class of bug the Royco fix and the polkadot-sdk `PrecompileDelegateDenied` pattern were both designed to close, just not applied here.

## Impact Explanation
`terminate` deletes the contract's code and account, transferring its remaining balance to an attacker-chosen `beneficiary` — this is the on-chain equivalent of `selfdestruct` in the original report. A contract author who delegatecalls into any third-party/library code (a legitimate and common Solidity pattern supported by `pallet-revive`) can have their contract irrecoverably terminated and its funds diverted, with no direct machine/privileged access needed by the attacker — satisfying the "unauthorized execution / theft or unbacked mint or unlock / permanent fund lock" impact class for a live pallet-revive scope.

## Likelihood Explanation
Exploitation requires only deploying an unprivileged malicious/compromised library contract and having any victim contract delegatecall into it (a normal usage pattern, not requiring governance, validator, or relayer collusion). This matches the "unauthenticated calls...from the wallet implementation contract" pattern in the seed report almost exactly, and the sibling precompiles in this same codebase were already assessed as needing this exact guard, indicating the vulnerability class is both realistic and already validated as security-relevant within this repository's own threat model.

## Recommendation
Add the same `is_delegate_call()` / `PrecompileDelegateDenied` guard used by `Storage`, `Assets`, `AssetConversion`, `Vesting`, and `XCM` precompiles to the `System` precompile's `call` entrypoint in `substrate/frame/revive/src/precompiles/builtin/system.rs`, rejecting delegatecall (or, for identity-returning calls, deriving identity from the non-delegated frame) before dispatching `terminate`, `callerIsRoot`, `callerIsOrigin`, and `ownCodeHash`.

## Proof of Concept
Conceptual reproduction (mirrors the Weiroll PoC, ported to pallet-revive semantics):
1. Deploy victim contract `A`, holding balance/state, whose logic legitimately uses `delegate_call` into a shared library address for gas-efficient computation (standard supported pattern per `HostFn::delegate_call` [11](#0-10) ).
2. Deploy malicious library `L` at the address `A` delegatecalls into (or compromise/upgrade the shared library `A` already trusts).
3. `L`'s code, executing with `A`'s account/storage context due to delegatecall, issues a **regular** `call` (not delegatecall) to the `System` precompile's fixed address `0x900` invoking `terminate(beneficiary)` [7](#0-6) .
4. Because no `is_delegate_call` guard exists on `System`, the call succeeds; `terminate_caller` resolves the caller as `A`'s frame and deletes `A`'s contract, sending its balance to `beneficiary` (attacker-controlled), without any check that `A`'s own top-level code authorized this.

### Citations

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

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L33-103)
```rust
impl<T: Config> BuiltinPrecompile for System<T> {
	type T = T;
	type Interface = ISystem::ISystemCalls;
	const MATCHER: BuiltinAddressMatcher =
		BuiltinAddressMatcher::Fixed(NonZero::new(0x900).unwrap());
	const HAS_CONTRACT_INFO: bool = false;

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
			ISystemCalls::hashBlake128(ISystem::hashBlake128Call { input }) => {
				env.frame_meter_mut()
					.charge_weight_token(RuntimeCosts::HashBlake128(input.len() as u32))?;
				let output = sp_io::hashing::blake2_128(input.as_bytes_ref());
				Ok(output.abi_encode())
			},
			ISystemCalls::toAccountId(ISystem::toAccountIdCall { input }) => {
				env.frame_meter_mut().charge_weight_token(RuntimeCosts::ToAccountId)?;
				let account_id = env.to_account_id(&H160::from_slice(input.as_slice()));
				Ok(account_id.encode().abi_encode())
			},
			ISystemCalls::callerIsOrigin(ISystem::callerIsOriginCall {}) => {
				env.frame_meter_mut().charge_weight_token(RuntimeCosts::CallerIsOrigin)?;
				let is_origin = env.caller_is_origin(true);
				Ok(is_origin.abi_encode())
			},
			ISystemCalls::callerIsRoot(ISystem::callerIsRootCall {}) => {
				env.frame_meter_mut().charge_weight_token(RuntimeCosts::CallerIsRoot)?;
				let is_root = env.caller_is_root(true);
				Ok(is_root.abi_encode())
			},
			ISystemCalls::ownCodeHash(ISystem::ownCodeHashCall {}) => {
				env.frame_meter_mut().charge_weight_token(RuntimeCosts::OwnCodeHash)?;
				let caller = env.caller();
				let addr = T::AddressMapper::to_address(caller.account_id()?);
				let output = env.code_hash(&addr.into()).0.abi_encode();
				Ok(output)
			},
			ISystemCalls::minimumBalance(ISystem::minimumBalanceCall {}) => {
				env.frame_meter_mut().charge_weight_token(RuntimeCosts::MinimumBalance)?;
				let minimum_balance = env.minimum_balance();
				Ok(minimum_balance.to_big_endian().abi_encode())
			},
			ISystemCalls::weightLeft(ISystem::weightLeftCall {}) => {
				env.frame_meter_mut().charge_weight_token(RuntimeCosts::WeightLeft)?;
				let ref_time = env.frame_meter().weight_left().unwrap_or_default().ref_time();
				let proof_size = env.frame_meter().weight_left().unwrap_or_default().proof_size();
				let res = (ref_time, proof_size);
				Ok(res.abi_encode())
			},
			ISystemCalls::terminate(ISystem::terminateCall { beneficiary }) => {
				// no need to adjust gas because this always deletes code
				env.frame_meter_mut()
					.charge_weight_token(RuntimeCosts::Terminate { code_removed: true })?;
				let h160 = H160::from_slice(beneficiary.as_slice());
				env.terminate_caller(&h160).map_err(Error::try_to_revert::<T>)?;
				Ok(Vec::new())
			},
```

**File:** substrate/frame/revive/src/precompiles/builtin/storage.rs (L43-51)
```rust
	) -> Result<Vec<u8>, Error> {
		// Benchmarks call the pre-compile functions directly, without the delegate
		// call overhead. The `delegate_call` overhead is benchmarked individually.
		#[cfg(not(feature = "runtime-benchmarks"))]
		if !env.is_delegate_call() {
			return Err(Error::Revert(
				"Storage precompile can only be called via delegate call".into(),
			));
		}
```

**File:** polkadot/xcm/pallet-xcm/precompiles/src/lib.rs (L78-81)
```rust
		frame_support::ensure!(
			!env.is_delegate_call(),
			pallet_revive::Error::<Self::T>::PrecompileDelegateDenied,
		);
```

**File:** substrate/frame/revive/src/exec.rs (L1972-2009)
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
	}
```

**File:** substrate/frame/revive/src/exec.rs (L2315-2334)
```rust
	fn caller(&self) -> Origin<T> {
		if let Some(Ok(mock_caller)) = self
			.exec_config
			.mock_handler
			.as_ref()
			.and_then(|mock_handler| mock_handler.mock_caller(self.frames.len()))
			.map(|mock_caller| Origin::<T>::from_runtime_origin(mock_caller))
		{
			return mock_caller;
		}

		if let Some(DelegateInfo { caller, .. }) = &self.top_frame().delegate {
			caller.clone()
		} else {
			self.frames()
				.nth(1)
				.map(|f| Origin::from_account_id(f.account_id.clone()))
				.unwrap_or(self.origin.clone())
		}
	}
```

**File:** substrate/frame/revive/uapi/src/host.rs (L213-221)
```rust
	fn delegate_call(
		flags: CallFlags,
		address: &[u8; 20],
		ref_time_limit: u64,
		proof_size_limit: u64,
		deposit_limit: &[u8; 32],
		input_data: &[u8],
		output: Option<&mut &mut [u8]>,
	) -> Result;
```
