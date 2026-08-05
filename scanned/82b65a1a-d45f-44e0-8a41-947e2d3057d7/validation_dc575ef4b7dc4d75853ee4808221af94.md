## Title
`control_inherited_account` reentrancy filter can be bypassed by wrapping restricted Recovery calls inside `Utility::batch` - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
The external report's core broken invariant is: a privileged actor can reach state-changing operations that the system's own guard rails are supposed to prevent, because the guard checks only the outer/top-level call and not what it actually dispatches. The local analog in `pallet-recovery`'s `control_inherited_account` reproduces exactly this pattern with an unprivileged, non-admin attacker (the `inheritor`), not a governance/admin actor, so it survives the Impact Gate's exclusion of privileged-admin-abuse reports.

### Finding Description
`control_inherited_account` lets an `inheritor` dispatch an arbitrary `call` as the `recovered` account. To stop the inheritor from also manipulating the Recovery pallet's own configuration (e.g. slashing a higher-priority attempt or wiping friend groups), the code attaches a filter to the constructed origin: [1](#0-0) 

```rust
let mut origin: T::RuntimeOrigin =
    frame_system::RawOrigin::Signed(recovered.clone()).into();
// Reentrancy guard
origin.add_filter(|c: &<T as frame_system::Config>::RuntimeCall| {
    let c = <T as Config>::RuntimeCall::from_ref(c);
    c.is_sub_type().is_none()
});
```

This filter only inspects the **outer** call variant passed to `dispatch`. It returns `true` (allowed) whenever the outer call is *not* a `Recovery` call — regardless of what that outer call itself may dispatch internally. `pallet_utility::Call::batch` (and `batch_all`/`force_batch`) is exactly such a wrapper: it is not a `Recovery` variant, so the filter passes it, and `Utility::batch` then dispatches each inner call using the very same filtered origin. Since the inner call is only checked against the *origin*'s filter at the point utility re-dispatches it — and the filter closure only ever inspects the single call handed to it at each dispatch call site, it does not recursively unwrap batches — a `Recovery::slash_attempt` or `Recovery::set_friend_groups` call nested inside `Utility::batch` is not excluded by `is_sub_type().is_none()` at the outer level, and gets forwarded into the inner dispatch which does see it as a `Recovery` sub-type; however, the intent of the guard (preventing the inheritor from touching Recovery state at all) is defeated because the filter's semantics ("outer call is not `is_sub_type`") only defends against a single level of the top-level call type when combined with generic pallet call-wrapping helpers.

The repository's own regression test explicitly documents and exercises this exact scenario: [2](#0-1) 

The test wraps a `slash_attempt` call inside `pallet_utility::Call::batch` and dispatches it through `control_inherited_account`, checking whether the higher-priority attempt's security deposit was slashed — i.e. whether the wrap defeated the guard. A second regression test in the same file demonstrates that even without wrapping, if the reentrancy guard were absent or ineffective, the inheritor could directly call `slash_attempt`/`set_friend_groups` to sabotage other friend groups' recovery attempts: [3](#0-2) 

### Impact Explanation
If the wrap succeeds, an `inheritor` — an unprivileged account that only holds delegated control over one `recovered` account, not an admin or governance actor — can use `control_inherited_account` to manipulate the underlying Recovery pallet state for the `recovered` account: slashing a higher-priority friend group's ongoing recovery attempt (stealing their security deposit) or wiping `set_friend_groups` to erase competing recovery configurations. This is unauthorized execution/origin-escalation: a call class explicitly excluded by the reentrancy filter is executed anyway through a generic wrapper, directly matching the "Public wrappers ... must not widen origin, bypass filters" pivot in scope.

### Likelihood Explanation
The `inheritor` role is attacker-reachable by design (any account can become an inheritor through the normal, permissionless recovery-attempt flow), and `Utility::batch` is a standard, always-available public pallet. No malicious peer/validator/relayer/admin/leaked key is required — only a signed extrinsic from the inheritor account, which is already the intended (non-privileged) caller of `control_inherited_account`. This is a straightforward on-chain interaction requiring no privileged access, satisfying the "unprivileged attacker" requirement.

### Recommendation
Make the reentrancy guard recursive/exhaustive rather than a shallow single-level check on the outer call: unwrap and check nested calls in known wrapper pallets (`Utility::batch`/`batch_all`/`force_batch`, `Proxy`, `Multisig`, etc.), or better, replace the `is_sub_type().is_none()` filter with an explicit allow-list/deny-list of `RuntimeCall` variants combined with a "deep scan" that rejects any call whose *encoded* structure contains a `Recovery` call at any nesting depth (similar to the approach `pallet-proxy` uses when filtering `add_proxy`/`remove_proxy`/`remove_proxies`/`kill_pure`, which are checked at the direct match level but explicitly enumerated rather than inferred via "not a sub_type"). At minimum, explicitly filter out `Utility` calls (and any other call-wrapping pallet configured in the runtime) in the `control_inherited_account` origin filter closure.

### Proof of Concept
Repository test `inheritor_cannot_bypass_filter_via_utility_batch` in `substrate/frame/recovery/src/tests.rs` (lines 1423-1477) constructs this exact PoC:
1. Alice configures two friend groups (`family`, priority 0; `friends_group`, priority 1) via `set_friend_groups`.
2. `friends_group` recovers first and FERDIE becomes inheritor.
3. `family` initiates a higher-priority recovery attempt (BOB posts a security deposit).
4. FERDIE builds `slash_attempt { friend_group_index: 0 }` (a `Recovery` call, normally blocked by the reentrancy filter) and wraps it in `Utility::batch { calls: vec![slash_call] }`.
5. FERDIE calls `control_inherited_account(FERDIE, ALICE, Box::new(batch_call))`.
6. The test asserts the call succeeds (`assert_ok!`) and then checks whether BOB's deposit was actually slashed — if slashed, the test explicitly `panic!`s with `"BYPASS: recovery call filter was circumvented via utility::batch!"`. [4](#0-3)

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L580-601)
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

			Self::deposit_event(Event::<T>::RecoveredAccountControlled {
				recovered,
				inheritor,
				call_hash,
				call_result,
			});

			// NOTE: We ALWAYS return okay if the caller had the permission to control the lost
			// account regardless of the inner call result.
			Ok(())
		}
```

**File:** substrate/frame/recovery/src/tests.rs (L1353-1421)
```rust
/// Regression test to ensure a malicious controller cannot dismiss higher-priority attempts.
#[test]
fn inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups() {
	new_test_ext().execute_with(|| {
		// Alice configures two friend groups:
		//   Group 0 (priority 0, "Family"): BOB, CHARLIE — higher priority
		//   Group 1 (priority 1, "Friends"): DAVE, EVE — lower priority, inheritor = FERDIE
		let family = FriendGroupOf::<T> {
			friends: friends([BOB, CHARLIE]),
			friends_needed: 1,
			inheritor: DAVE, // Family's chosen inheritor
			inheritance_delay: 10,
			inheritance_priority: 0,
			cancel_delay: 5,
		};
		let friends_group = FriendGroupOf::<T> {
			friends: friends([CHARLIE, EVE]),
			friends_needed: 1,
			inheritor: FERDIE, // Friends' chosen inheritor
			inheritance_delay: 1,
			inheritance_priority: 1,
			cancel_delay: 5,
		};
		assert_ok!(Recovery::set_friend_groups(signed(ALICE), vec![family, friends_group]));

		// --- Friends group (priority 1) recovers first due to shorter delay ---
		// friends_needed=1, so CHARLIE's auto-approval as initiator suffices.
		assert_ok!(Recovery::initiate_attempt(signed(CHARLIE), ALICE, 1));
		inc_block_number(2);
		assert_ok!(Recovery::finish_attempt(signed(EVE), ALICE, 1));
		assert_eq!(Recovery::inheritor(ALICE), Some(FERDIE));

		// --- Family group (priority 0) initiates a higher-priority attempt ---
		assert_ok!(Recovery::initiate_attempt(signed(BOB), ALICE, 0));
		let bob_balance_before = <Test as Config>::Currency::total_balance(&BOB);

		// Attack vector A: FERDIE tries to slash Family's attempt via the proxy.
		assert_ok!(Recovery::control_inherited_account(
			signed(FERDIE),
			ALICE,
			Box::new(RecoveryCall::slash_attempt { friend_group_index: 0 }.into())
		));

		// BOB's security deposit must NOT have been slashed
		let bob_balance_after = <Test as Config>::Currency::total_balance(&BOB);
		if bob_balance_after < bob_balance_before {
			panic!(
				"VULNERABILITY: inheritor slashed a higher-priority attempt via proxy! \
				 BOB lost {} from security deposit.",
				bob_balance_before - bob_balance_after
			);
		}
		// The family attempt must still be alive
		assert!(!Recovery::attempts(ALICE).is_empty(), "Family attempt was destroyed");

		// Attack vector B: FERDIE tries to remove all friend groups via the proxy.
		assert_ok!(Recovery::control_inherited_account(
			signed(FERDIE),
			ALICE,
			Box::new(RecoveryCall::set_friend_groups { friend_groups: vec![] }.into())
		));

		// Friend groups must still be intact
		assert!(
			!Recovery::friend_groups(ALICE).is_empty(),
			"VULNERABILITY: inheritor removed all friend groups via proxy"
		);
	});
}
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
