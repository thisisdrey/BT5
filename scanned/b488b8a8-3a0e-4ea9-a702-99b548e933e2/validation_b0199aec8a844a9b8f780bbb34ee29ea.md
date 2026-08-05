### Title
Missing delegatecall guard on `System` precompile's `terminate()` lets an attacker-controlled callee destroy the top-level caller's contract and redirect its funds - (File: `substrate/frame/revive/src/precompiles/builtin/system.rs`)

### Summary
The Mimo report shows a callback (`executeOperation`) that only checks `msg.sender == lendingPool` but never verifies *who initiated* the flash loan, letting an attacker route attacker-controlled parameters through a trusted callback to move victim funds. `pallet-revive` has an analogous class of bugs that was explicitly fixed for several precompiles (`ERC20` assets, `asset-conversion`, `xcm`, `vesting`) by adding a `PrecompileDelegateDenied` guard, because those precompiles derive identity from `env.caller()`, which is misleading under `DELEGATECALL`. That fix (see `pr_11715.prdoc`) was applied to four precompiles but the builtin `System` precompile at fixed address `0x900`, which also derives caller-bound state and exposes `terminate()` (a SELFDESTRUCT-equivalent that sends the executing contract's remaining balance to an attacker-supplied `beneficiary`), was left without any `is_delegate_call()` check.

### Finding Description
`System::call` in `substrate/frame/revive/src/precompiles/builtin/system.rs` implements `ISystemCalls::terminate`: [1](#0-0) 

Unlike the `Storage` precompile, which explicitly **requires** `is_delegate_call()`: [2](#0-1) 

and unlike `ERC20`, `asset-conversion`, `vesting`, and `pallet-xcm` precompiles, which **deny** delegatecall specifically because `env.caller()` is misleading in that context: [3](#0-2) [4](#0-3) 

`System::call` has **no delegatecall check at all**. Under `DELEGATECALL`, code executes in the storage/account frame of the top-level caller, not the immediate callee. `terminate_caller` operates on the currently executing frame's own account — which, in a delegatecall chain, is the *original* (possibly victim) contract, not the intermediary contract that issued the nested delegatecall. The PR that introduced the guard for the other four precompiles explicitly documents the underlying invariant that is violated here: "Delegatecall to precompiles allows a malicious contract to execute precompile logic in a misleading caller context... There is no legitimate use case for delegatecalling into these precompiles." [5](#0-4) 

This is the same broken invariant as the Mimo bug: a state-changing entry point trusts the immediate execution context (`msg.sender`/frame identity) without verifying that it was reached through an authorized call path, letting an attacker inject fully attacker-controlled parameters (`beneficiary`) into a privileged operation (fund-redirecting self-destruct) executed against someone else's account.

### Impact Explanation
If a victim contract (e.g. a proxy, vault, or any contract that delegatecalls into logic it does not fully control — a common pattern for upgradeable contracts, plugin modules, or "diamond"-style multi-facet contracts) delegatecalls into attacker-influenced code, that code can nest a further `DELEGATECALL` into precompile `0x900` and invoke `terminate(attackerBeneficiary)`. Because the frame/account context is inherited unchanged through delegatecall chains, this destroys the *victim's* contract and sends its full remaining native-token balance to the attacker-chosen address — a direct, unbacked theft of contract-held value with no privileged actor, governance, or off-chain assumption required, matching "theft ... or unbacked mint or unlock" / "permanent user-fund ... lock" in the impact gate.

### Likelihood Explanation
Exploitability requires only an ordinary contract-composition pattern already common in EVM ecosystems (delegatecall-based proxies/libraries) and does not require any malicious validator, collator, relayer, or governance action — it is a pure public-entrypoint, unprivileged-attacker path once a victim contract delegatecalls into code the attacker can steer (a realistic and frequently audited-for scenario in Solidity, e.g. the historical Parity multisig library self-destruct incident). The guard pattern was already recognized as necessary and applied to four sibling precompiles in this very codebase, indicating the risk class is acknowledged but this one entry point was missed.

### Recommendation
Add the same `PrecompileDelegateDenied` guard used by `ERC20`, `asset-conversion`, `vesting`, and `pallet-xcm` precompiles to the `System` precompile, at minimum for `terminate` (and any other call that mutates or destroys frame/account state), e.g.:
```rust
frame_support::ensure!(
    !env.is_delegate_call(),
    crate::Error::<T>::PrecompileDelegateDenied,
);
```
placed at the top of `System::call`, mirroring `substrate/frame/assets/precompiles/src/lib.rs:168-171`.

### Proof of Concept
1. Deploy `Victim`, a contract that at some point does `address(Module).delegatecall(data)` where `Module` is a facet/library address that can be pointed at attacker-supplied code (e.g. via an upgrade path, a configurable module registry, or because `Module` itself is attacker-deployed and unvetted).
2. Attacker's `Module` code, when reached, issues `address(0x900).delegatecall(abi.encodeWithSelector(ISystem.terminate.selector, attackerAddress))`.
3. Because `System::call` performs no `is_delegate_call()` check, `terminate_caller` executes against `Victim`'s own frame/account (the context is inherited unchanged through the delegatecall chain).
4. `Victim`'s contract code is removed and its full native balance is transferred to `attackerAddress`, exactly mirroring the "callback trusts context, not origin of the invocation chain" defect in the Mimo `executeOperation()` finding.

### Citations

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

**File:** substrate/frame/assets/precompiles/src/lib.rs (L163-172)
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

**File:** polkadot/xcm/pallet-xcm/precompiles/src/lib.rs (L73-81)
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
