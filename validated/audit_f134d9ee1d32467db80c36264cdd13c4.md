## Finding: Built-in `System` precompile in `pallet-revive` is missing the delegatecall-origin guard added to sibling precompiles

### Title
Missing `PrecompileDelegateDenied` guard on the built-in `System` precompile allows caller-context spoofing during delegatecall - (File: `substrate/frame/revive/src/precompiles/builtin/system.rs`)

### Summary
The Rubicon `FeeWrapper` bug is a "shared-caller-context" vulnerability: a generic forwarding contract loses the true end-user identity, so `msg.sender` seen by the downstream contract is the wrapper, not the actual user, letting an attacker manipulate state/permissions that were meant to belong to someone else. `pallet-revive`'s precompile framework has the exact same bug class in its caller-identity API (`env.caller()`), and Parity already fixed it for several precompiles in the very same codebase — but the built-in `System` precompile (always present at fixed address `0x900` in every `pallet-revive` runtime) was not updated.

### Finding Description
`env.caller()` returns the original external caller even during a delegatecall, i.e. it does not distinguish "who actually authored this call" from "which contract's storage/code frame is executing". PR `pr_11715` (`prdoc/stable2606/pr_11715.prdoc`) documents this precisely:

> "Delegatecall to precompiles allows a malicious contract to execute precompile logic in a misleading caller context. The precompiles derive caller identity from `env.caller()`, which during delegatecall returns the original caller — letting the intermediary contract act on the caller's assets or send XCM on their behalf."

That PR added an `is_delegate_call()` / `PrecompileDelegateDenied` guard to:
- `substrate/frame/assets/precompiles/src/lib.rs`
- `substrate/frame/asset-conversion/precompiles/src/lib.rs`
- `polkadot/xcm/pallet-xcm/precompiles/src/lib.rs` [1](#0-0) 

and `substrate/frame/vesting/precompiles/src/lib.rs` independently implements the same `ensure_mutable` guard [2](#0-1) .

The **built-in `System` precompile**, `substrate/frame/revive/src/precompiles/builtin/system.rs`, is part of `pallet-revive` core (not one of the crates bumped by PR 11715), lives at fixed address `0x900`, and is available in every runtime that enables `pallet-revive`. It uses `env.caller()` directly with no delegatecall check at all:

- `ownCodeHash` resolves the caller's account id via `env.caller()` and returns the code hash at that address [3](#0-2) 
- `callerIsOrigin` and `callerIsRoot` are explicit authorization/identity primitives (the on-chain analog of Solidity's `tx.origin`/root checks) that downstream contracts are expected to use to gate privileged logic, and they too rely on unguarded caller resolution [4](#0-3) 

Unlike `assets`, `asset-conversion`, `xcm`, and `vesting` precompiles, `System::call` never checks `env.is_delegate_call()` before answering identity queries.

### Impact Explanation
This is the direct analog of the `FeeWrapper` primitive: a victim contract `V` delegatecalls into an attacker-supplied library `L` (a common pattern for logic upgrades/plugins). Inside that delegatecall frame, `env.caller()` still resolves to whoever originally called `V` (e.g. a privileged/root/governance-controlled account), not to `L`. If `L` invokes `System.callerIsRoot()` / `System.callerIsOrigin()` / `System.ownCodeHash()`, it obtains `V`'s real caller identity and can use that answer to satisfy access-control checks that were only meant to authorize `V` itself — enabling unauthorized execution of privileged code paths inside `V`'s frame that the attacker does not actually hold the authority for. This matches the required impact class "unauthorized execution or origin escalation" via a public wrapper (delegatecall) that "widen[s] origin" without the guard other sibling precompiles already enforce.

### Likelihood Explanation
No privileged actor, relayer, validator, or governance action is required. Any unprivileged attacker who can get a target contract to `delegatecall` into attacker-influenced code (a very common integration pattern for "library"/"logic" contracts on `pallet-revive`) can trigger this. The fact that Parity already recognized and fixed this exact bug class for four other precompiles in the same PR, but left the always-present `System` precompile unpatched, indicates the fix was incomplete rather than the risk being assessed as low.

### Recommendation
Add the same guard used elsewhere to `System::call` in `substrate/frame/revive/src/precompiles/builtin/system.rs`:
```rust
frame_support::ensure!(
    !env.is_delegate_call(),
    crate::Error::<T>::PrecompileDelegateDenied,
);
```
at least for the caller-identity-dependent branches (`ownCodeHash`, `callerIsOrigin`, `callerIsRoot`, and `terminate`), consistent with the guard already present in `pallet-xcm`, `pallet-assets`, `pallet-asset-conversion`, and `pallet-vesting` precompiles.

### Proof of Concept
1. Deploy victim contract `V` whose privileged function `adminOnly()` checks `System.callerIsRoot()` (or checks `ownCodeHash()`/`callerIsOrigin()` as an authorization signal) before executing sensitive logic, and which also exposes a delegatecall-based plugin mechanism (e.g. `execute(address lib, bytes calldata data) external { lib.delegatecall(data); }`), a common upgradeable/library pattern.
2. An unprivileged attacker deploys malicious library `L` whose code calls into the `0x900` System precompile's `callerIsRoot()`/`ownCodeHash()` and then invokes `V`'s privileged internal logic.
3. A privileged/root account interacts with `V` (e.g., calls `V.execute(L, data)` as part of normal governance flow, or is tricked into it), causing `V` to delegatecall `L`.
4. Inside `L`'s delegatecall frame, `env.caller()` still reports the root/privileged account as caller (since delegatecall does not update caller in `pallet-revive`'s `Ext`), so `System.callerIsRoot()` returns `true` even though the attacker's code `L`, not the root account, is actually driving execution.
5. `L` uses this false-positive authorization signal to execute privileged logic within `V`'s frame that the attacker was never authorized to trigger directly — mirroring how the `FeeWrapper` let an attacker manipulate state (`cancel`, `transfer`) that was only supposed to be actionable by the true offer-owner, because the downstream contract could not distinguish real caller identity from the forwarding contract's context. [5](#0-4)

### Citations

**File:** polkadot/xcm/pallet-xcm/precompiles/src/lib.rs (L78-87)
```rust
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

**File:** substrate/frame/revive/src/precompiles/builtin/system.rs (L67-76)
```rust
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
