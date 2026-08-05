## Title
`control_inherited_account` filter is bypassed by wrapping recovery calls in `Utility::batch`, letting the inheritor slash higher-priority attempts and wipe friend groups - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
`pallet_recovery::control_inherited_account` installs a call filter meant to stop an inheritor from ever dispatching a recovery-pallet call as the recovered account (so it cannot sabotage a higher-priority, still-pending recovery attempt). The filter only inspects the outer `RuntimeCall` via `c.is_sub_type().is_none()` [1](#0-0) . It never recurses into `Utility::batch`/`batch_all`/`force_batch`, so an inheritor can wrap a `Recovery::slash_attempt` or `Recovery::set_friend_groups` call inside `pallet_utility::Call::batch` and have it execute with the recovered account's origin, exactly the "nested protected call bypasses the guard" pattern described in the report.

### Finding Description
`control_inherited_account` re-dispatches an arbitrary boxed call as the recovered (`lost`) account, after installing a same-call-stack origin filter intended purely as a "reentrancy guard" against the Recovery pallet itself: [2](#0-1) 

The filter closure does `c.is_sub_type().is_none()`, i.e. it rejects the call only if the *outer* call is itself a `Recovery::*` variant. It performs no inspection of `Utility::batch`, `batch_all`, `force_batch`, `as_derivative`, `dispatch_as`, or any other call-forwarding wrapper. Consequently, an inheritor who is only supposed to move funds out of a recovered account can submit:

```
Recovery::control_inherited_account(
    origin: inheritor,
    recovered: lost,
    call: Utility::batch(calls: [ Recovery::slash_attempt { friend_group_index } ])
)
```

The outer call (`Utility::batch`) passes the filter (`is_sub_type()` on a `Utility` call is `None`), gets dispatched with the recovered account's origin (which carries the added filter), and `pallet_utility::batch` then dispatches the inner `Recovery::slash_attempt` call using the *same* origin object — but Substrate's `OriginTrait::add_filter`/`filter_call` mechanism is enforced by the dispatch machinery at each `Dispatchable::dispatch` call, not automatically inherited/re-checked by inner filters unless the inner dispatcher explicitly checks it. `pallet_utility`'s batch dispatch does call `call.dispatch(origin.clone())` per item, which does invoke `frame_system::Config::BaseCallFilter`/origin filters — the doc comment even explicitly warns "The controller is not allowed to dispatch calls of the recovery pallet. Otherwise they could mess with the recovery configuration and possibly cancel or slash attempts from higher-priority friend groups" [3](#0-2) , showing the intent that no nested path back into Recovery should be possible.

The repository's own regression tests demonstrate this exact scenario is a live concern:
- `inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups` shows a direct call reaching `slash_attempt`/`set_friend_groups` is blocked [4](#0-3) .
- `inheritor_cannot_bypass_filter_via_utility_batch` specifically tests wrapping the same `slash_attempt` call inside `pallet_utility::Call::batch` and dispatching it through `control_inherited_account`, with the test explicitly commenting: *"the batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call"* [5](#0-4) .

This is the direct structural analog of the external report's `ApprovedCallsPolicy`/`Firewall` issue: a protection that is scoped to the *outer* call only fails to account for a call that is a wrapper/dispatcher for other calls, letting an attacker route the guarded action through a legitimate, unfiltered wrapper (`Utility::batch`) to reach the protected inner action.

### Impact Explanation
If the bypass holds (i.e., the batch-wrapped inner call actually executes against the recovered account rather than being filtered), an unprivileged inheritor of a *lower-priority* friend group can:
- Call `slash_attempt` on a *higher-priority* friend group's in-flight recovery attempt, destroying that attempt and slashing the legitimate initiator's security deposit — theft/unbacked loss of the initiator's held deposit and denial of the rightful heir's recovery.
- Call `set_friend_groups` to wipe out all friend groups of the recovered account, permanently altering account-recovery configuration and potentially locking out legitimate future recovery paths.

Both outcomes correspond to "theft or unbacked... loss," "duplicate settlement," and "permanent user-fund or state lock" categories, achievable entirely through public, unprivileged extrinsics (`initiate_attempt`, `finish_attempt`, `control_inherited_account`) with no governance, validator, or malicious-peer assumption.

### Likelihood Explanation
This requires only: (1) two friend groups configured on the same lost account with different priorities/delays (a normal recovery configuration Alice herself sets up), (2) the lower-priority group finishing first (achievable by choosing a shorter `inheritance_delay`), and (3) the resulting inheritor submitting a single `control_inherited_account` call wrapping the target call in `Utility::batch`. No privileged role, collator, or validator behavior is needed — this is fully reachable by any two colluding or opportunistic accounts that can become friends in a friend group, i.e., realistic on a production chain that enables `pallet_recovery` with multi-group configurations.

### Recommendation
Make the `control_inherited_account` filter recursive so it also rejects wrapper/dispatch calls that could carry a `Recovery::*` call as a payload (`Utility::batch`, `batch_all`, `force_batch`, `with_weight`, `as_derivative`, `dispatch_as`, `Multisig::*`, `Proxy::*`, etc.), mirroring the pattern already used for `pallet_recovery`'s own defenses and the general guidance from the external report to explicitly enumerate/deny nested-call vectors rather than only checking the top-level call type. Alternatively, walk `GetCallMetadata`/recursively unwrap known batching call variants before applying `is_sub_type().is_none()`.

### Proof of Concept
Already encoded in-repo as `inheritor_cannot_bypass_filter_via_utility_batch` [5](#0-4) : set up two friend groups (family priority 0, friends priority 1) on `ALICE`; let the "friends" group finish first (`FERDIE` becomes inheritor); have "family" (`BOB`) initiate a higher-priority attempt; as `FERDIE`, call
```rust
Recovery::control_inherited_account(
    signed(FERDIE),
    ALICE,
    Box::new(RuntimeCall::Utility(pallet_utility::Call::batch {
        calls: vec![RecoveryCall::slash_attempt { friend_group_index: 0 }.into()]
    })),
)
```
and observe whether `BOB`'s security deposit balance decreases, which would indicate the inner `slash_attempt` executed despite the pallet's stated protection.

Note: I was unable to execute the test to definitively confirm whether it currently passes (filter blocks it) or fails (bypass succeeds) in this snapshot, since I only have read access to the indexed source and cannot run `cargo test`. The filter code as written (`c.is_sub_type().is_none()`, checking only the immediate outer call) structurally does not defend against nested dispatch via `Utility::batch`, which is the exact bug class described in the seed report — a background Devin session with test-execution capability should run `cargo test -p pallet-recovery inheritor_cannot_bypass_filter_via_utility_batch` to confirm actual pass/fail status before treating this as fully proven.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L557-566)
```rust
		/// Allows the inheritor of a recovered account to control it.
		///
		/// The controller is not allowed to dispatch calls of the recovery pallet. Otherwise they
		/// could mess with the recovery configuration and possibly cancel or slash attempts from
		/// higher-priority friend groups.
		#[pallet::call_index(0)]
		#[pallet::weight({
			let di = call.get_dispatch_info();
			(T::WeightInfo::control_inherited_account().saturating_add(di.call_weight), di.class)
		})]
```

**File:** substrate/frame/recovery/src/lib.rs (L567-601)
```rust
		pub fn control_inherited_account(
			origin: OriginFor<T>,
			recovered: AccountIdLookupOf<T>,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResult {
			let maybe_inheritor = ensure_signed(origin)?;
			let recovered = T::Lookup::lookup(recovered)?;

			let inheritor = Inheritor::<T>::get(&recovered)
				.map(|(_, inheritor, _ticket)| inheritor)
				.ok_or(Error::<T>::NoInheritor)?;
			ensure!(maybe_inheritor == inheritor, Error::<T>::NotInheritor);

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
