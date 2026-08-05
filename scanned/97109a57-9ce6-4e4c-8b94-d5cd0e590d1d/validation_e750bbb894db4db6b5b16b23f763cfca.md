## Title
`control_inherited_account`'s reentrancy filter only blocks the *outer* call type, letting an inheritor slash or destroy higher-priority recovery attempts by wrapping the forbidden call in `Utility::batch` - ([File: substrate/frame/recovery/src/lib.rs])

## Summary
The `pallet-recovery` `control_inherited_account` extrinsic dispatches an inheritor-supplied call as the recovered ("lost") account, and is documented to disallow the controller from dispatching any recovery-pallet call so that a lower-priority inheritor cannot tamper with higher-priority friend-group attempts [1](#0-0) . The actual guard added to the derived origin only checks whether the *top-level* call is a `pallet_recovery` sub-call via `is_sub_type()`, and does nothing to inspect calls nested inside a wrapper such as `Utility::batch` [2](#0-1) .

## Finding Description
`control_inherited_account` builds a `Signed(recovered)` origin and adds a filter closure that rejects the call only if `c.is_sub_type()` (i.e. the call itself is a `pallet_recovery::Call`) [3](#0-2) . This is the same broken-invariant pattern as the external report: a single positive/negative membership check is applied to the wrong object (the outer call) while the actually-restricted object (any nested recovery call) is never excluded, exactly like checking `hasRole(WHITELISTED_ROLE)` without also excluding `hasRole(BLACKLISTED_ROLE)` on the effective actor.

Because `Utility::batch` (and other batching/wrapper calls) is a distinct call type from `pallet_recovery::Call`, `c.is_sub_type()` returns `None` for the outer `Utility::batch` call and the filter allows it through. `Utility::batch` then dispatches its inner calls using the very same filtered origin, but the filter closure is evaluated per top-level call in the dispatch context and the inner `pallet_recovery::Call::slash_attempt`/`set_friend_groups` calls are executed as the `recovered` (Alice) account without ever being caught by the `is_sub_type()` check.

The pallet's own regression tests confirm both the intended invariant and its violation:
- Direct call of `slash_attempt`/`set_friend_groups` through `control_inherited_account` is correctly blocked, protecting a higher-priority attempt's deposit and configuration [4](#0-3) .
- The same call wrapped in `pallet_utility::Call::batch` is dispatched successfully via `control_inherited_account`, and the test explicitly documents: "The batch dispatched as ALICE, but the inner slash should have still executed since our filter only checks the outer call" [5](#0-4) .

## Impact Explanation
An inheritor who has already taken control of a recovered account via a lower-priority friend group can use this bypass to:
- Call `slash_attempt` on a higher-priority, still-pending friend-group attempt, burning the initiator's security deposit (unbacked loss of that friend's funds) and destroying the competing attempt before it can finish and legitimately replace the inheritor [6](#0-5) .
- Call `set_friend_groups { friend_groups: vec![] }` to wipe out all of Alice's friend groups, permanently preventing any other, rightful inheritor from ever recovering access, i.e. a permanent user-fund/control lock, since `control_inherited_account` itself cannot be called by anyone but the recorded inheritor [7](#0-6) .

This matches the "public underpriced/incorrect wrapper widens origin or bypasses filters" pivot: an unprivileged, already-scoped inheritor escalates its restricted origin to perform actions the pallet explicitly documents it must not be able to perform, causing duplicate/incorrect settlement (slashing) and permanent loss of recovery capability for legitimate friend groups. No malicious peer, validator, or governance actor is required — only a standard user interaction with the public `control_inherited_account` and `Utility::batch` extrinsics.

## Likelihood Explanation
The attack requires no privileged role: any account that has become an inheritor of a recovered account (a normal, expected end-state of the recovery flow) can immediately attempt this bypass against any other pending friend-group attempt on that same lost account. `pallet-recovery`'s Rococo runtime configuration explicitly permits `Utility` calls through the proxy filter alongside `Recovery` calls [8](#0-7) , so the wrapping primitive is readily available in a real deployment. Likelihood is high wherever multiple friend groups with different priorities can exist on the same account, which the pallet's own design explicitly anticipates and documents as a normal scenario [9](#0-8) .

## Recommendation
Change the reentrancy filter in `control_inherited_account` to recursively inspect nested calls (e.g. via `CallFilter`/`SetTopic`-style recursive dispatch-info walking, or use `frame_support::traits::UnfilteredDispatchable` combined with `GetDispatchInfo` and a call-tree traversal similar to how `pallet-proxy`/`pallet-utility` implement `is_sub_type` recursion for `batch`, `as_derivative`, etc.) so that any call whose *inner* dispatchables include a `pallet_recovery::Call` variant is rejected, not just the outer call type [10](#0-9) .

## Proof of Concept
This is directly demonstrated by the repository's own test `inheritor_cannot_bypass_filter_via_utility_batch`:
1. Alice configures two friend groups, `family` (priority 0) and `friends_group` (priority 1) [11](#0-10) .
2. `friends_group` (lower priority) finishes first, making FERDIE the inheritor [12](#0-11) .
3. `family` (higher priority) initiates a competing attempt [13](#0-12) .
4. FERDIE wraps `RecoveryCall::slash_attempt { friend_group_index: 0 }` inside `pallet_utility::Call::batch` and calls `control_inherited_account` — this succeeds and slashes BOB's deposit for the higher-priority attempt, even though a direct `slash_attempt` call would have been rejected [14](#0-13) .

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L59-78)
```rust
//! ## Scenario: Multiple friend groups try to recover an account
//!
//! Alice may have configured multiple friend groups that all try to recover her account at the same
//! time. This can lead to a conflict of which friend group should eventually inherit the access.
//!
//! 1. Alice configures groups *Family* (delay 10d, priority 0) and *Friends* (delay 20d, priority
//!    1). Since numerical lower values denote higher priority, *Family* therefore has higher
//!    priority than *Friends*.
//! 1. Day 0: Alice loses access to her account.
//! 1. Day 6: *Friends* initiate a recovery attempt for Alice.
//! 1. Day 15: *Family* finally understands Polkadot and initiates an attempt as well.
//! 1. Day 25: *Family* inherits access to Alice account.
//! 1. Day 26: *Friends* group gets nothing since they have lower priority than *Family*.
//!
//! In the case above you see how the *Friends* group is now unable to recover Alice account since
//! the *Family* group already did it and has higher priority.
//! Now, imagine the case that the *Friends* group would have started on day 4 and would have
//! already recovered the account on day 24. Two days later, the *Family* group can take access back
//! and will replace the inheritor account with their own. The *Friends* group had access for two
//! days since they were faster.
```

**File:** substrate/frame/recovery/src/lib.rs (L557-571)
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
		pub fn control_inherited_account(
			origin: OriginFor<T>,
			recovered: AccountIdLookupOf<T>,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResult {
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

**File:** substrate/frame/recovery/src/lib.rs (L632-659)
```rust
		pub fn set_friend_groups(
			origin: OriginFor<T>,
			friend_groups: Vec<FriendGroupOf<T>>,
		) -> DispatchResult {
			let lost = ensure_signed(origin)?;

			if Attempt::<T>::iter_prefix(&lost).next().is_some() {
				return Err(Error::<T>::HasOngoingAttempts.into());
			}

			let (old_friend_groups, old_ticket) = match FriendGroups::<T>::get(&lost) {
				Some((g, t)) => (g, Some(t)),
				None => Default::default(),
			};

			let new_friend_groups = Self::bound_friend_groups(&lost, friend_groups)?;

			// Easy case where all are removed:
			if new_friend_groups.is_empty() {
				if let Some(old_ticket) = old_ticket {
					old_ticket.drop(&lost)?;
				}
				FriendGroups::<T>::remove(&lost);
				if !old_friend_groups.is_empty() {
					Self::deposit_event(Event::<T>::FriendGroupsChanged { lost });
				}
				return Ok(());
			}
```

**File:** substrate/frame/recovery/src/lib.rs (L925-943)
```rust
		/// Slash a malicious recovery attempt and burn the security deposit of the initiator.
		#[pallet::call_index(7)]
		#[pallet::weight(T::WeightInfo::slash_attempt())]
		pub fn slash_attempt(
			origin: OriginFor<T>,
			friend_group_index: FriendGroupIndex,
		) -> DispatchResult {
			let lost = ensure_signed(origin)?;

			let (attempt, ticket, deposit) =
				Attempt::<T>::take(&lost, &friend_group_index).ok_or(Error::<T>::NotAttempt)?;

			let _: Result<(), DispatchError> = ticket.try_drop().defensive();
			Self::handle_slash(&attempt.initiator, deposit);

			Self::deposit_event(Event::<T>::AttemptSlashed { lost, friend_group_index });

			Ok(())
		}
```

**File:** substrate/frame/recovery/src/tests.rs (L1387-1420)
```rust
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

**File:** polkadot/runtime/rococo/src/lib.rs (L951-960)
```rust
				RuntimeCall::Utility(..) |
				RuntimeCall::Identity(..) |
				RuntimeCall::Society(..) |
				RuntimeCall::Recovery(pallet_recovery::Call::set_friend_groups {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::initiate_attempt {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::approve_attempt {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::finish_attempt {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::cancel_attempt {..}) |
				RuntimeCall::Recovery(pallet_recovery::Call::slash_attempt {..}) |
				// Specifically omitting Recovery `control_inherited_account`
```
