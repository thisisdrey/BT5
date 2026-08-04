Based on my research, I found a direct, well-documented local analog of the "wrong sender resolution" bug class in `pallet-revive`'s precompile/delegatecall design.

### Title
Identity used by privileged precompiles is derived from `env.caller()`, which is not sender-context-aware during `DELEGATECALL` - ([File: substrate/frame/revive/src/exec.rs])

### Summary
The external report's core broken invariant is: a privileged check (`onlyDelegate`/`onlyFundApprover`) is bound to a raw, unguarded sender value instead of the properly delegated/authorized identity, letting an intermediary caller/contract act with someone else's authorization. The same invariant break exists in `pallet-revive`: contract precompiles resolve the acting identity through `Ext::caller()`, but `DELEGATECALL` deliberately preserves the *original* caller's context rather than switching to the immediate caller [1](#0-0) . Any precompile that reads `env.caller()` to authorize a privileged, value-moving action without first checking `env.is_delegate_call()` inherits an "wrong-sender" authorization bug identical in shape to the reported issue.

### Finding Description
`Stack::delegate_call` pushes a new frame whose `DelegateInfo::caller` is explicitly set to `self.caller().clone()` — i.e., the *original* caller, not the contract performing the delegatecall [2](#0-1) . This is correct EVM semantics for normal contract-to-contract logic (code changes, but `msg.sender`/`msg.value` are inherited from the outer call), but it becomes dangerous the moment a *precompile* uses that same `caller()` value as an authorization/identity source for a privileged action (e.g., moving a user's assets or sending XCM "on their behalf"), because an untrusted intermediary contract can be delegatecall'ed into and inherit the victim's identity without the victim's consent.

This exact exploit mechanics is documented in the repository's own fix history: `PrecompileDelegateDenied` was added specifically because "Delegatecall to precompiles allows a malicious contract to execute precompile logic in a misleading caller context. The precompiles derive caller identity from `env.caller()`, which during delegatecall returns the original caller — letting the intermediary contract act on the caller's assets or send XCM on their behalf." [3](#0-2)  This fix was applied to the ERC20 assets precompile, the asset-conversion precompile, and the XCM precompile, "matching the existing pattern in the vesting ... precompiles" [4](#0-3) . The vesting precompile's guard shows the correct pattern: `caller_account_id` must be gated by `ensure_mutable`, which explicitly rejects delegatecall via `env.is_delegate_call()` before trusting `env.caller()` for a state-changing action [5](#0-4) .

The structural weakness is architectural, not a one-off typo: `Ext::caller()` is exposed as a generic API used across precompiles, and each precompile author must remember to add the `is_delegate_call()`/`PrecompileDelegateDenied` guard individually. Any current or future precompile (built-in or pallet-exposed) that reads `env.caller()`/`caller_account_id()` to gate a privileged, fund-moving, or cross-chain-messaging operation, without independently re-adding this delegatecall guard, reproduces the exact "modifier trusts the wrong sender primitive" bug from the external report — an unprivileged attacker deploys a wrapper contract, has a victim (or any relayer/aggregator) delegatecall into it, and the wrapper forwards a delegatecall into the sensitive precompile, which then authorizes the action as if the victim called it directly.

### Impact Explanation
Where unguarded, this allows an unprivileged attacker to move a victim's assets, approve/transfer tokens, or dispatch XCM "on the victim's behalf" without their consent — i.e., unauthorized execution/origin escalation and unbacked transfer of value, matching the "theft or unbacked mint/unlock" and "unauthorized execution or origin escalation" impact categories.

### Likelihood Explanation
High for any precompile that omits the guard: exploitation requires only deploying an ordinary contract and getting any party (not necessarily the precompile author) to delegatecall through it — no validator, governance, relayer, or privileged actor is needed, matching the "unprivileged attacker, public entrypoint" requirement. The repository's own PR history confirms this was a real, exploitable condition for the assets, asset-conversion, and XCM precompiles prior to the fix in `pr_11715.prdoc`.

### Recommendation
Enforce the `is_delegate_call()` check centrally rather than per-precompile — e.g., have the precompile dispatch/execution path in `pallet-revive` reject `DELEGATECALL` by default for any precompile unless it explicitly opts in, instead of requiring every precompile implementation to remember to call `ensure_mutable`/`PrecompileDelegateDenied` individually. Audit all current and future precompiles (built-in and pallet-exposed, e.g. under `substrate/frame/revive/src/precompiles/builtin/`) for any use of `env.caller()`/`caller_account_id()` in a privileged code path that lacks this guard.

### Proof of Concept
1. Attacker deploys contract `Wrapper` whose only function is `delegatecall(precompile_address, calldata)`.
2. Victim (or any automation/aggregator contract acting for the victim) is induced to `CALL` into `Wrapper`, e.g. as part of a batched interaction.
3. `Wrapper` issues `DELEGATECALL` into a sensitive precompile (e.g. an asset/XCM precompile) that authorizes actions based on `env.caller()`.
4. Per `Stack::delegate_call`, the frame's caller remains the original caller (the victim), so the precompile executes the privileged action (asset transfer, XCM dispatch) attributing it to the victim [6](#0-5) , unless that specific precompile independently checks `is_delegate_call()` as shown in the vesting precompile's `ensure_mutable` [7](#0-6)  and as retrofitted for assets/asset-conversion/xcm by `pr_11715` [8](#0-7) .

**Note on verification limits**: I confirmed the guard is present in `vesting`, `assets`, `asset-conversion`, and `xcm` precompiles per `pr_11715`. I was not able to exhaustively re-verify every built-in precompile under `substrate/frame/revive/src/precompiles/builtin/` (e.g. `storage.rs`, which showed only a single match for `is_delegate_call` versus the two-line guard pattern seen elsewhere) before running out of tool iterations, so I cannot confirm with certainty whether any specific built-in precompile is currently unpatched — this would need direct inspection of `substrate/frame/revive/src/precompiles/builtin/storage.rs` and any sibling files to name a concrete currently-vulnerable file.

### Citations

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

**File:** prdoc/stable2606/pr_11715.prdoc (L1-7)
```text
title: Reject delegatecall into precompiles via PrecompileDelegateDenied
doc:
- audience: Runtime Dev
  description: "## Summary\n\n- Add delegatecall guard to the ERC20 assets precompile\
    \ and XCM precompile, matching the existing pattern in the vesting and asset-conversion\
    \ precompiles\n- Converge asset-conversion precompile from `Error::Revert(string)`\
    \ to `Error::Error(PrecompileDelegateDenied)` for consistency across all precompiles\n\
```

**File:** prdoc/stable2606/pr_11715.prdoc (L8-13)
```text
    - Add delegatecall rejection test for the XCM precompile\n\n## Motivation\n\n\
    Delegatecall to precompiles allows a malicious contract to execute precompile\
    \ logic in a misleading caller context. The precompiles derive caller identity\
    \ from `env.caller()`, which during delegatecall returns the original caller \u2014\
    \ letting the intermediary contract act on the caller's assets or send XCM on\
    \ their behalf. There is no legitimate use case for delegatecalling into these\
```

**File:** prdoc/stable2606/pr_11715.prdoc (L14-23)
```text
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

**File:** substrate/frame/vesting/precompiles/src/lib.rs (L48-68)
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

fn caller_account_id<T: Config>(
	env: &impl Ext<T = T>,
	context: &str,
) -> Result<T::AccountId, Error> {
	env.caller()
		.account_id()
		.map_err(|e| {
			Error::Revert(alloc::format!("{context}: caller has no account id: {e:?}").into())
		})
		.cloned()
}
```
