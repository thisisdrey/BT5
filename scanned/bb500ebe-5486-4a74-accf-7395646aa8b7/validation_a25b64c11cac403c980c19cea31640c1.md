Based on the investigation, the strongest local analog to the `tx.origin` phishing pattern is in **`pallet-revive`'s built-in precompile framework**, specifically the missing `is_delegate_call()` guard on the `System` precompile's `terminate` function.

### Title
Missing delegatecall guard on `System` precompile `terminate` allows identity-forwarding fund drain via untrusted delegatecall target - (File: `substrate/frame/revive/src/precompiles/builtin/system.rs`)

### Summary
`pallet-revive` precompiles derive the "authorized" identity from `env.caller()`. Under a `delegatecall`, `env.caller()` (and the executing account/balance context) resolves to the *delegating* contract's identity, not the immediate contract that issued the call into the precompile — structurally identical to the `tx.origin` vs `msg.sender` confusion in the external report. The project already recognized and fixed this exact bug class in four other precompiles (`assets`, `asset-conversion`, `xcm`, `vesting`) via PR `#11715` ("Reject delegatecall into precompiles via `PrecompileDelegateDenied`"), which added an `is_delegate_call()` check before any privileged, caller-identity-based action. The built-in `System` precompile (`substrate/frame/revive/src/precompiles/builtin/system.rs`) was not included in that fix and still lacks any such guard, even though it exposes `terminate`, a privileged, state-changing, caller-identity-bound operation.

### Finding Description
The `Vesting`, `Assets`, `Asset-Conversion`, and `Xcm` precompiles all now call `ensure_mutable`/an equivalent guard that rejects execution when `env.is_delegate_call()` is true, specifically because: [1](#0-0) 
This was motivated by: "the precompiles derive caller identity from `env.caller()`, which during delegatecall returns the original caller — letting the intermediary contract act on the caller's assets or send XCM on their behalf" (`prdoc/stable2606/pr_11715.prdoc`).

The `System` builtin precompile at fixed address `0x900` implements the same `env.caller()`-derived-identity pattern but has **no** `is_delegate_call()` check anywhere in the file: [2](#0-1) 

Critically, it exposes a privileged, state-mutating `terminate` selector that acts on the calling frame's identity and lets the caller pick an arbitrary beneficiary: [3](#0-2) 

Because `System::HAS_CONTRACT_INFO` is `false`, calls into this precompile execute purely within the calling frame's account/storage/balance context. If contract `X` performs a `delegatecall` into an untrusted or later-compromised logic contract `L` (e.g. a diamond/proxy pattern, a pluggable module, or a library `X`'s author trusted for unrelated functionality), `L`'s code executes with `X`'s identity (`env.caller()`/`env.address()` == `X`). `L` can then invoke the `System` precompile's `terminate` selector with an attacker-controlled `beneficiary`, causing `X`'s own contract to self-destruct and its entire remaining balance to be sent to the attacker — without `X`'s own top-level code ever intending or authorizing that action. This is the exact "forward the trusted identity through an intermediary to bypass access control" primitive from the `tx.origin` report, just realized through `delegatecall` identity-preservation instead of `tx.origin` preservation across an external call.

### Impact Explanation
This allows unauthorized theft/loss of contract-held value: a contract's entire native balance can be redirected to an attacker-chosen address and the contract irrecoverably destroyed, triggered purely by the contract's own (possibly legitimate) choice to `delegatecall` into code that turns out to be malicious or later compromised — without any admin, validator, or off-chain compromise required. This falls squarely within the "theft or unbacked mint or unlock" / "permanent user-fund ... lock" impact categories for `pallet-revive` execution.

### Likelihood Explanation
Likelihood is significant because: (1) delegatecall-based proxy/diamond/library patterns are common in EVM-compatible tooling being ported to `pallet-revive`; (2) the project has already treated this exact bug class as security-relevant and patched four sibling precompiles for it in `#11715`, confirming the pattern is realistic and exploitable; (3) no privileged action or governance access is needed — only that some contract chooses to delegatecall into code that is or becomes attacker-influenced.

### Recommendation
Add the same `is_delegate_call()` guard used in `ensure_mutable` (as in `substrate/frame/vesting/precompiles/src/lib.rs`) to the `System` precompile's `terminate` handler in `substrate/frame/revive/src/precompiles/builtin/system.rs`, rejecting with `PrecompileDelegateDenied` when invoked via delegatecall. More generally, audit every builtin precompile (including `substrate/frame/revive/src/precompiles/builtin/storage.rs`, which also references `env.caller()`) for the same missing guard, since the fix in `#11715` was applied ad hoc to four crate-based precompiles rather than centrally in the `Precompile`/`BuiltinPrecompile` dispatch path.

### Proof of Concept
1. Deploy contract `X` with a normal balance and a code path that performs `delegatecall` to an address `L` supplied/upgradable by its owner (e.g., a proxy pattern).
2. Deploy `L` (initially benign, or swapped in later by a compromised upgrade key/storage slot) whose code, when delegatecalled, issues a `call` to the fixed `System` precompile address (`0x900`) with input `terminate(beneficiary=<attacker>)`.
3. Trigger `X`'s delegatecall path. Because `HAS_CONTRACT_INFO = false` and no `is_delegate_call()` check exists in `terminate`'s handler, `env.terminate_caller(&attacker)` executes within `X`'s frame identity, destroying `X` and transferring its full balance to `attacker`.
4. Compare with the now-patched `Vesting`/`Assets`/`Xcm`/`Asset-Conversion` precompiles, where the identical call pattern is rejected with `PrecompileDelegateDenied` before any state-mutating logic runs.

### Citations

**File:** substrate/frame/vesting/precompiles/src/lib.rs (L48-56)
```rust
fn ensure_mutable<T: Config>(env: &impl Ext<T = T>) -> Result<(), Error> {
	if env.is_read_only() {
		return Err(pallet_revive::Error::<T>::StateChangeDenied.into());
	}
	if env.is_delegate_call() {
		return Err(pallet_revive::Error::<T>::PrecompileDelegateDenied.into());
	}
	Ok(())
}
```

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L40-49)
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
