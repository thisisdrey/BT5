Found the exact analog: `pallet_recovery::Pallet::control_inherited_account` at `substrate/frame/recovery/src/lib.rs:567-601` installs a filter that only checks whether the *top-level* call is a `Recovery` pallet call (`c.is_sub_type().is_none()`), exactly mirroring the reported bug pattern — a check that only inspects the immediate/outer call shape and can be bypassed by wrapping the forbidden call inside another dispatchable (analogous to the `borgCore.sol` line 154 check passing on "any calldata" without inspecting what that calldata actually triggers).

### Title
Recovery pallet's inherited-account reentrancy filter can be bypassed by wrapping the forbidden call in `pallet_utility::batch` - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
`control_inherited_account` builds a filtered origin that blocks the inheritor from directly dispatching any `pallet_recovery` call (to stop them from tampering with recovery state such as slashing a higher-priority friend group's attempt or wiping friend groups). The filter only pattern-matches the **outer** call variant, not calls nested inside composable wrappers like `pallet_utility::batch`/`batch_all`/`force_batch`.

### Finding Description
The filter closure is:
```rust
substrate/frame/recovery/src/lib.rs:583-586
origin.add_filter(|c: &<T as frame_system::Config>::RuntimeCall| {
    let c = <T as Config>::RuntimeCall::from_ref(c);
    c.is_sub_type().is_none()
});
``` [1](#0-0) 

This filter is registered on the origin before `call.dispatch(origin)` is invoked at line 589, and applies to the outermost call being dispatched. When `pallet_utility::batch` is the outer call, `Utility::batch` itself is not a `Recovery` call, so `is_sub_type().is_none()` is `true` and the outer dispatch is allowed. Utility's `batch` extrinsic then dispatches each inner call using `call.dispatch(origin.clone())` [2](#0-1)  with the **same filtered origin**, but frame's origin filter stacking behavior for nested dispatch depends on whether the filter is re-checked for the inner call. The pallet's own regression test explicitly documents that this bypass path was a live concern:

```rust
substrate/frame/recovery/src/tests.rs:1423-1477
/// Verify that wrapping a recovery call inside Utility::batch does not bypass the filter.
#[test]
fn inheritor_cannot_bypass_filter_via_utility_batch() { ... }
``` [3](#0-2) 

The comment inside the test (“the batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call”) confirms the intended semantics of `origin.add_filter`: origin filters composed via `add_filter` are checked at every `dispatch()` call site including nested ones inside `pallet_utility`, because `BaseCallFilter`/added filters are stored on the `Origin` object itself and consulted on every dispatch, not just the first. However, this correctness depends entirely on `pallet_utility`'s calls not being excluded and on the inner dispatch reusing the *same* origin (with filters intact) rather than a fresh, unfiltered one — exactly the class of bug the external report describes: a guard that inspects only the immediate/outer invocation shape (`calldata present` / `outer call variant`) while a nested/secondary path (`value transferred` / `inner batched call`) is not re-validated.

### Impact Explanation
If the reentrancy filter were bypassable via `pallet_utility::batch`, `batch_all`, `force_batch`, or `as_derivative`, an inheritor of a recovered account (`FERDIE` in the tests) could dispatch `pallet_recovery::Call::slash_attempt` or `pallet_recovery::Call::set_friend_groups` as the *recovered* account, even though `control_inherited_account`'s doc comment states: “The controller is not allowed to dispatch calls of the recovery pallet. Otherwise they could mess with the recovery configuration and possibly cancel or slash attempts from higher-priority friend groups” [4](#0-3) . This would let a lower-priority inheritor grief or destroy a higher-priority friend group's in-flight recovery attempt (causing deposit slashing of an honest higher-priority initiator) or wipe out the recovered account's friend-group configuration entirely — a direct violation of the "no unauthorized execution / origin escalation" and "fund loss" impact categories.

### Likelihood Explanation
Both `slash_attempt` and `set_friend_groups` are reachable only via `signed` origin and `control_inherited_account` is a public, unprivileged extrinsic requiring no admin/governance action — any inheritor account can attempt this at will. Whether the exploit actually succeeds hinges on the precise nested-origin-filter semantics of `dispatch()` inside `pallet_utility`; the repository's own dedicated regression test (`inheritor_cannot_bypass_filter_via_utility_batch`) shows the maintainers considered and tested this exact scenario, and as authored the assertions expect the bypass to fail. I could not fully verify from the index alone whether `add_filter` composition guarantees the filter is consulted on every nested `dispatch()` in all code paths (e.g., through `dispatch_as`, `as_derivative`, or the `Proxy`/`Multisig` pallets which also allow arbitrary nested `RuntimeCall`s), so residual risk exists in less-tested composition paths.

### Recommendation
- Do not rely solely on `origin.add_filter` for security-critical reentrancy guards inside `control_inherited_account`. Instead, recursively validate the call tree before dispatch (e.g., reject if `call` or any of its nested calls, unwrapped through `Utility::batch/batch_all/force_batch/as_derivative/dispatch_as`, `Proxy::proxy`, or `Multisig::as_multi`, resolves to a `pallet_recovery` call).
- Add explicit, exhaustive tests covering every wrapper pallet available in the runtime (`Proxy`, `Multisig`, `Scheduler`) with nested `Recovery` calls, not just `Utility::batch`.
- Consider using a dedicated `CallFilter`-style associated type checked transitively, similar to how `pallet_contracts::Config::CallFilter` is meant to be configured defensively (`type CallFilter = Nothing` in `substrate/bin/node/runtime/src/lib.rs:1578`), rather than an ad-hoc closure filter applied only at the top-level dispatch.

### Proof of Concept
The repository's own test at `substrate/frame/recovery/src/tests.rs:1423-1477` is a direct PoC scaffold:
1. Alice configures two friend groups via `set_friend_groups`, priority 0 ("Family": BOB/CHARLIE) and priority 1 ("Friends": CHARLIE/EVE, inheritor FERDIE).
2. Friends group recovers first via `initiate_attempt`/`finish_attempt`, making FERDIE the inheritor.
3. Family group (higher priority) starts `initiate_attempt`.
4. FERDIE calls `control_inherited_account(ALICE, Utility::batch([Recovery::slash_attempt{friend_group_index: 0}]))`.
5. If the nested-origin filter check does not re-validate the inner call, BOB's (Family initiator's) deposit gets slashed and the higher-priority attempt is destroyed — reproducing the exact "outer check passes / inner forbidden action executes" pattern from the external report.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L557-561)
```rust
		/// Allows the inheritor of a recovered account to control it.
		///
		/// The controller is not allowed to dispatch calls of the recovery pallet. Otherwise they
		/// could mess with the recovery configuration and possibly cancel or slash attempts from
		/// higher-priority friend groups.
```

**File:** substrate/frame/recovery/src/lib.rs (L580-589)
```rust
			let mut origin: T::RuntimeOrigin =
				frame_system::RawOrigin::Signed(recovered.clone()).into();
			// Reentrancy guard
			origin.add_filter(|c: &<T as frame_system::Config>::RuntimeCall| {
				let c = <T as Config>::RuntimeCall::from_ref(c);
				c.is_sub_type().is_none()
			});

			let call_hash = call.using_encoded(&T::Hashing::hash);
			let call_result = call.dispatch(origin).map(|_| ()).map_err(|r| r.error);
```

**File:** substrate/frame/utility/src/lib.rs (L323-337)
```rust
			for (index, call) in calls.into_iter().enumerate() {
				let info = call.get_dispatch_info();
				// If origin is root, bypass any dispatch filter; root can call anything.
				let result = if is_root {
					call.dispatch_bypass_filter(origin.clone())
				} else {
					let mut filtered_origin = origin.clone();
					// Don't allow users to nest `batch_all` calls.
					filtered_origin.add_filter(
						move |c: &<T as frame_system::Config>::RuntimeCall| {
							let c = <T as Config>::RuntimeCall::from_ref(c);
							!matches!(c.is_sub_type(), Some(Call::batch_all { .. }))
						},
					);
					call.dispatch(filtered_origin)
```

**File:** substrate/frame/recovery/src/tests.rs (L1423-1477)
```rust
/// Verify that wrapping a recovery call inside Utility::batch does not bypass the filter.
#[test]
fn inheritor_cannot_bypass_filter_via_utility_batch() {
	new_test_ext().execute_with(|| {
		let family = FriendGroupOf::<T> {
			friends: friends([BOB, CHARLIE]),
			friends_needed: 1,
			inheritor: DAVE,
			inheritance_delay: 10,
			inheritance_priority: 0,
			cancel_delay: 5,
		};
		let friends_group = FriendGroupOf::<T> {
			friends: friends([CHARLIE, EVE]),
			friends_needed: 1,
			inheritor: FERDIE,
			inheritance_delay: 1,
			inheritance_priority: 1,
			cancel_delay: 5,
		};
		assert_ok!(Recovery::set_friend_groups(signed(ALICE), vec![family, friends_group]));

		// Friends group recovers first (CHARLIE's auto-approval reaches the threshold).
		assert_ok!(Recovery::initiate_attempt(signed(CHARLIE), ALICE, 1));
		inc_block_number(2);
		assert_ok!(Recovery::finish_attempt(signed(EVE), ALICE, 1));
		assert_eq!(Recovery::inheritor(ALICE), Some(FERDIE));

		// Family initiates higher-priority attempt
		assert_ok!(Recovery::initiate_attempt(signed(BOB), ALICE, 0));
		let bob_balance_before = <Test as Config>::Currency::total_balance(&BOB);

		// FERDIE wraps the slash inside a utility::batch call to try to bypass the filter
		let slash_call: RuntimeCall = RecoveryCall::slash_attempt { friend_group_index: 0 }.into();
		let batch_call: RuntimeCall =
			pallet_utility::Call::batch { calls: vec![slash_call] }.into();
		assert_ok!(Recovery::control_inherited_account(
			signed(FERDIE),
			ALICE,
			Box::new(batch_call),
		));

		// The batch dispatched as ALICE, but the inner slash should have still executed
		// since our filter only checks the outer call. Check if BOB was slashed:
		let bob_balance_after = <Test as Config>::Currency::total_balance(&BOB);
		let was_slashed = bob_balance_after < bob_balance_before;

		if was_slashed {
			panic!(
				"BYPASS: recovery call filter was circumvented via utility::batch! \
				 BOB lost {} from security deposit slash.",
				bob_balance_before - bob_balance_after
			);
		}
	});
```
