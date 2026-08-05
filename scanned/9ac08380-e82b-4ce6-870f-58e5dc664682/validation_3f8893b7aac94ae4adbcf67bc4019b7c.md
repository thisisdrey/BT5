## Title
`System::ownCodeHash` precompile returns the wrong code hash under `delegatecall`, allowing self-identity checks to be bypassed - (File: `substrate/frame/revive/src/precompiles/builtin/system.rs`)

## Summary
This is the direct local analog of the zkSync ECRECOVER/`delegatecall` bug: a precompile's output silently changes meaning depending on the call type (`call` vs `delegatecall`) because the precompile resolves an "identity" value from the execution context instead of from the target it is supposed to describe. In `pallet-revive`, the `System` precompile's `ownCodeHash()` function is implemented using `env.caller()`, whose semantics are context-dependent on whether the current frame is a delegate-call frame or not, causing the returned "own code hash" to silently refer to a different account than the one actually executing when reached via `delegatecall`.

## Finding Description
`ISystemCalls::ownCodeHash` is implemented as: [1](#0-0) 

It fetches `env.caller()`, maps it to an `H160`, and looks up `env.code_hash()` for that address — i.e. it returns the code hash of the *caller*, not of the executing contract itself, despite the name `ownCodeHash` implying it should describe the account whose code is currently running (analogous to Solidity's `address(this).codehash`).

`Stack::caller()` in `exec.rs` is explicitly context-sensitive to delegate-call framing: [2](#0-1) 

If the top frame is a delegate-call frame (`DelegateInfo` is set), `caller()` returns the *original* caller that was captured when the delegate call was initiated — not the immediate calling contract, and not the currently executing contract's own account. This is exactly the same broken invariant as the ECRECOVER report: a "precompile"-style call changes what value gets returned/used purely based on whether it was reached through `delegatecall`, rather than the meaning the name promises.

This class of caller-identity confusion under `delegatecall` is already recognized as a real vulnerability pattern in this codebase — PR #11715 added a `PrecompileDelegateDenied` guard to the ERC20 assets, asset-conversion, vesting, and XCM precompiles specifically because "the precompiles derive caller identity from `env.caller()`, which during delegatecall returns the original caller — letting the intermediary contract act on the caller's assets or send XCM on their behalf": [3](#0-2) 

However, `System::ownCodeHash` was not included in that hardening pass and has no `env.is_delegate_call()` guard at all, unlike `Storage` (address `0x901`), which explicitly rejects non-delegate calls: [4](#0-3) 

`System` (address `0x900`) has no such check anywhere in its `call` implementation: [5](#0-4) 

## Impact Explanation
Any Solidity/EVM contract calling `System.ownCodeHash()` to obtain "my own code hash" (e.g., to implement a self-verification/authenticity check, an anti-proxy guard, or a governance check gated on a known immutable code hash) will get a silently incorrect value whenever the call path passes through a `delegatecall` frame. Because `env.caller()` resolves to `DelegateInfo.caller` (the original outer caller) rather than the account whose code is actually executing, a contract author cannot reliably distinguish "this is really my code hash" from "this is some unrelated account's code hash" — matching the report's core impact: silent, context-dependent divergence from the intended/expected semantics with no revert, which can compromise logic relying on this value (e.g. self-authentication, anti-spoofing, contract-identity gating).

## Likelihood Explanation
No privileged actor is required: any unprivileged contract deployer can trigger this simply by calling the `System` precompile through a `delegatecall` frame (directly, or by being delegate-called into by another contract), which is a completely ordinary, permissionless EVM-compatibility code path in `pallet-revive`. The bug is deterministic and always reproducible, not probabilistic — same class of "context reachable, no guard" issue that upstream authors already treated as worth a dedicated fix (`PrecompileDelegateDenied`) for other precompiles.

## Recommendation
Add the same guard pattern used by `Storage` and the assets/XCM/vesting/asset-conversion precompiles to `System::ownCodeHash` (and audit other `System` calls that rely on `env.caller()`), either by rejecting delegate calls with `PrecompileDelegateDenied`, or — better, since the function is meant to describe "self" — by using `env.account_id()` and mapping via `T::AddressMapper` instead of `env.caller()`, so the returned hash always refers to the code that is actually executing regardless of call type.

## Proof of Concept
1. Deploy contract `A` with known code hash `H_A`.
2. Deploy contract `B` that calls `System.ownCodeHash()` via `delegatecall` on address `0x900`, inside a `delegatecall` invoked by contract `A` (i.e., `A` delegate-calls into `B`, and `B` delegate-calls the `System` precompile).
3. In `Stack::caller()`, since the top frame's `delegate` is `Some(DelegateInfo{ caller: A's original caller, .. })`, the resolved caller is neither `A` nor `B`, but `A`'s original caller — so `ownCodeHash()` returns the code hash of that unrelated account instead of `H_A` (or `B`'s own hash), silently returning a value inconsistent with any expected "self code hash" semantic, exactly mirroring the ECRECOVER `delegatecall` PoC where the returned value diverged from the non-delegate-call baseline.

### Citations

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L40-46)
```rust
	fn call(
		_address: &[u8; 20],
		input: &Self::Interface,
		env: &mut impl Ext<T = Self::T>,
	) -> Result<Vec<u8>, Error> {
		use ISystem::ISystemCalls;
		match input {
```

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L77-83)
```rust
			ISystemCalls::ownCodeHash(ISystem::ownCodeHashCall {}) => {
				env.frame_meter_mut().charge_weight_token(RuntimeCosts::OwnCodeHash)?;
				let caller = env.caller();
				let addr = T::AddressMapper::to_address(caller.account_id()?);
				let output = env.code_hash(&addr.into()).0.abi_encode();
				Ok(output)
			},
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

**File:** substrate/frame/revive/src/precompiles/builtin/storage.rs (L44-51)
```rust
		// Benchmarks call the pre-compile functions directly, without the delegate
		// call overhead. The `delegate_call` overhead is benchmarked individually.
		#[cfg(not(feature = "runtime-benchmarks"))]
		if !env.is_delegate_call() {
			return Err(Error::Revert(
				"Storage precompile can only be called via delegate call".into(),
			));
		}
```
