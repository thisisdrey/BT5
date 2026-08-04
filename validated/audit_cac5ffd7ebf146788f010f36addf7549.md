Based on my research, I found a strong local analog in `pallet-revive`'s address-derivation / nonce-bump logic, which mirrors the exact bug class from the report: a critical invariant (a "settlement/callback" side-effect) is only enforced through a code path that is gated by an optional, caller-supplied configuration flag, and the flag's effect is inconsistent between the "origin is caller" case and the nested/recursive case.

### Title
Inconsistent nonce-bump gating between `bump_nonce` config and recursion depth causes `create1` address mismatch / duplicate-contract collision - (File: `substrate/frame/revive/src/exec.rs`)

### Summary
`pallet-revive`'s `exec.rs` derives a new contract's `CREATE1` address from the deployer's account nonce, and decides whether to increment that nonce with the expression `if bump_nonce || !is_first_frame`. This is structurally identical to the Atlas bug: a mandatory bookkeeping step (bumping the nonce / verifying `reconcile()`) is executed only when one of two independent, code-path-specific conditions is true (`needsPreSolver`/`needsSolverPostCall` in Atlas vs. `bump_nonce`/`is_first_frame` in revive), and the address-derivation logic elsewhere assumes the bump *always* happened for "origin is caller" transactions.

### Finding Description
In `substrate/frame/revive/src/exec.rs` (lines ~1141-1163), when preparing an `Instantiate` frame, the deployment address is computed as: [1](#0-0) 

This unconditionally assumes: *"the Nonce from the origin has been incremented pre-dispatch, so we need to subtract 1 to get the nonce at the time of the call"* whenever `origin_is_caller` is true.

However, the actual nonce increment for the *caller* happens later, gated by: [2](#0-1) 

This increment is conditioned on `bump_nonce || !is_first_frame` — i.e., it is skipped when `bump_nonce == false` **and** `is_first_frame == true`. `bump_nonce` is a field of `ExecConfig` that is explicitly set to `false` for Ethereum-originated transactions and for the "without bump" substrate variant: [3](#0-2) 

The `origin_is_caller` predicate used in the address-derivation subtraction is a separate signal from `bump_nonce`/`is_first_frame`; nothing in the code ties these two conditions together to guarantee they always agree. The PR history in `prdoc/stable2506/pr_8504.prdoc` and `prdoc/stable2509/pr_8829.prdoc` shows this exact family of bugs was already hit twice (dry-run vs. real-tx address mismatch, and Eth-tx double-bump), confirming that the nonce-bump/derive-address invariant is fragile and has repeatedly diverged in this codebase — the same underlying "checks don't cover all situations" class as the Atlas report, where an optional/config-gated step silently doesn't happen on some code paths while a downstream computation still assumes it did. [4](#0-3) [5](#0-4) 

### Impact Explanation
If `origin_is_caller` can be true while `bump_nonce` is `false` and `is_first_frame` is `true` for some caller frame in a code path not yet covered by the two known/fixed cases, the address-derivation subtraction (`account_nonce - 1`) would compute an address using a nonce value that was never actually bumped for that call, producing:
- A `CREATE1` address that does not correspond to the account nonce that will actually be stored on-chain, enabling a collision with a *future* legitimately-derived contract address at the same nonce (`DuplicateContract`) or, more critically, silent address confusion between the caller's intended deployment and an attacker-influenced one.
- Given contract addresses are used as capability/identity anchors for held funds and permissions in `pallet-revive`, an address mismatch of this kind can misdirect value or execution to the wrong contract instance.

This matches the "Polkadot SDK Pivots" instruction that public wrappers/contracts execution must not allow incorrect settlement or duplicate/misdirected state.

### Likelihood Explanation
This is reachable by any unprivileged caller through the standard `instantiate`/`instantiate_with_code` extrinsics and the Ethereum-compatibility dispatch path (`eth_instantiate_with_code`), with no special privilege required — only the specific combination of `origin_is_caller`, `bump_nonce`, and `is_first_frame` needs to occur. Because this exact class of divergence between "the nonce we assume was bumped" and "the nonce we actually bumped" has already caused two separate confirmed bugs (PR #8504, PR #8829) in this very file, the underlying invariant is demonstrably fragile and not centrally enforced by a single source of truth — it is instead re-derived independently in at least two places (`origin_is_caller` check at address-derivation time vs. `bump_nonce || !is_first_frame` at nonce-increment time).

### Recommendation
Do not duplicate the "was this nonce bumped" decision. Compute the effective bump decision once (e.g., a single boolean derived from `bump_nonce` and frame depth) and use that single value both for the actual `inc_account_nonce` call and for the `-1` adjustment in `create1` address derivation, so the two can never diverge. Add a regression test that specifically instantiates with `origin_is_caller == true` combined with each `(bump_nonce, is_first_frame)` combination and asserts the derived address always matches the nonce that ends up persisted in storage.

### Proof of Concept
A concrete PoC requires access to the runtime/test harness to enumerate frame-depth and `ExecConfig` combinations exhaustively (this exceeds what static code reading can prove without executing the test suite). What is established from the repository evidence: (1) the address derivation formula subtracts 1 whenever `origin_is_caller`, unconditionally, at `exec.rs:1152-1157`; (2) the actual bump is separately gated by `bump_nonce || !is_first_frame` at `exec.rs:1359-1363`; (3) two previous PRs (#8504, #8829) fixed real instances where these two independent checks fell out of sync. I was not able to fully verify from the index alone whether a *currently unfixed* third combination exists (e.g., a nested `Instantiate` frame reached via `origin_is_caller = true` with `is_first_frame = true` and `bump_nonce = false` through some call path other than eth-tx/dry-run) — this would require running the pallet-revive test suite or tracing all callers of `Stack::run_instantiate`/`ExecConfig` construction to confirm definitively. I flag this uncertainty explicitly: a Devin session with terminal access should enumerate all `ExecConfig` construction sites and all callers reaching `Instantiate` frames to confirm whether `origin_is_caller` can be true under `bump_nonce=false, is_first_frame=true` outside the already-patched eth-tx/dry-run paths.

### Citations

**File:** substrate/frame/revive/src/exec.rs (L1141-1158)
```rust
			FrameArgs::Instantiate { sender, executable, salt, input_data } => {
				let deployer = T::AddressMapper::to_address(&sender);
				let account_nonce = <System<T>>::account_nonce(&sender);
				let address = if let Some(salt) = salt {
					address::create2(&deployer, executable.code(), input_data, salt)
				} else {
					use sp_runtime::Saturating;
					address::create1(
						&deployer,
						// the Nonce from the origin has been incremented pre-dispatch, so we
						// need to subtract 1 to get the nonce at the time of the call.
						if origin_is_caller {
							account_nonce.saturating_sub(1u32.into()).saturated_into()
						} else {
							account_nonce.saturated_into()
						},
					)
				};
```

**File:** substrate/frame/revive/src/exec.rs (L1359-1363)
```rust
				if bump_nonce || !is_first_frame {
					// Needs to be incremented before calling into the code so that it is visible
					// in case of recursion.
					<System<T>>::inc_account_nonce(caller.account_id()?);
				}
```

**File:** substrate/frame/revive/src/primitives.rs (L471-505)
```rust
impl<T: Config> ExecConfig<T> {
	/// Create a default config appropriate when the call originated from a substrate tx.
	pub fn new_substrate_tx() -> Self {
		Self {
			bump_nonce: true,
			collect_deposit_from_hold: None,
			effective_gas_price: None,
			is_dry_run: None,
			mock_handler: None,
			test_env_transient_storage: None,
		}
	}

	pub fn new_substrate_tx_without_bump() -> Self {
		Self {
			bump_nonce: false,
			collect_deposit_from_hold: None,
			effective_gas_price: None,
			mock_handler: None,
			is_dry_run: None,
			test_env_transient_storage: None,
		}
	}

	/// Create a default config appropriate when the call originated from a ethereum tx.
	pub fn new_eth_tx(effective_gas_price: U256, encoded_len: u32, base_weight: Weight) -> Self {
		Self {
			bump_nonce: false,
			collect_deposit_from_hold: Some((encoded_len, base_weight)),
			effective_gas_price: Some(effective_gas_price),
			mock_handler: None,
			is_dry_run: None,
			test_env_transient_storage: None,
		}
	}
```

**File:** prdoc/stable2506/pr_8504.prdoc (L9-24)
```text
    The issue stems from the `create1` address derivation logic in `exec.rs`:

    ```rust
    address::create1(
        &deployer,
        // the Nonce from the origin has been incremented pre-dispatch, so we
        // need to subtract 1 to get the nonce at the time of the call.
        if origin_is_caller {
            account_nonce.saturating_sub(1u32.into()).saturated_into()
        } else {
            account_nonce.saturated_into()
        },
    )
    ```

    The code correctly subtracts 1 from the account nonce during a transaction execution (because the nonce is incremented pre-dispatch), but doesn't account for execution context - whether it's a real transaction or a dry run through the RPC.
```

**File:** prdoc/stable2509/pr_8829.prdoc (L1-7)
```text
title: Eth transaction do not double bump the nonce
doc:
- audience: Runtime Dev
  description: |-
    Add an extra `eth_instantiate_with_code` dispatchable that is used by the EVM compatibility layer and ensures that the origin's nonce is only incremented once

    Fixes https://github.com/paritytech/contract-issues/issues/64
```
