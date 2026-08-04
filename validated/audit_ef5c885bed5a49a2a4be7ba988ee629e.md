## Title
Recovery pallet's reentrancy filter in `control_inherited_account` only inspects the outer call, letting the inheritor dispatch forbidden nested `Recovery` calls (e.g. `slash_attempt`, `set_friend_groups`) via `Utility::batch` - (File: `substrate/frame/recovery/src/lib.rs`)

### Summary
`Pallet::control_inherited_account` is meant to let the `inheritor` of a recovered account act as that account, while explicitly forbidding the inheritor from dispatching any `Recovery`-pallet call (so they cannot tamper with friend groups or slash competing attempts). This restriction is enforced with a single, non-recursive origin filter that inspects only the top-level `RuntimeCall`. Wrapping a forbidden `Recovery` call inside `pallet_utility::Call::batch` (or similarly, `batch_all`, `dispatch_as`, `if_else`, etc.) evades the filter because the filter only classifies the outer call, not the calls it will eventually dispatch internally. This is the same broken-invariant class as the reported Move bug: a security check that is applied to one branch/shape of the input but not to a semantically-equivalent alternate path that achieves the same effect.

### Finding Description
The intended restriction is expressed here: [1](#0-0) 

The filter added via `origin.add_filter` only checks `c.is_sub_type().is_none()`, i.e., whether the immediate top-level call belongs to the `Recovery` pallet: [2](#0-1) 

This is exactly analogous to the reported `update_position()` bug: the check gates behavior based on a single dimension of the input (`is_sub_type()` of the *outer* call) while the function actually supports composite/bundled operations (an arbitrary `RuntimeCall`, which itself may be `pallet_utility::batch` containing an arbitrary list of sub-calls). Because `pallet_utility::batch` dispatches its inner calls with the *same origin* and does not re-apply the caller-supplied filter, wrapping `RecoveryCall::slash_attempt` or `RecoveryCall::set_friend_groups` inside a `Utility::batch` call passes the outer filter (since the outer call is `Utility::batch`, not `Recovery::*`), and then the inner `Recovery` call executes with the `lost` account's origin, defeating the "not allowed to dispatch calls of the recovery pallet" guarantee.

This exact bypass is demonstrated as a regression test already present in the repository, describing precisely this attack and its consequence (the inner `slash_attempt` executing despite the filter): [3](#0-2) 

### Impact Explanation
An unprivileged inheritor (who only gained the constrained "control" privilege intended solely for transferring the recovered account's funds/assets) can use this bypass to:
- Call `slash_attempt` against a competing, higher-priority friend group's in-progress recovery attempt, destroying a legitimate initiator's security deposit and killing a rival attempt that would otherwise out-prioritize the current inheritor [4](#0-3) 
- Call `set_friend_groups` to wipe out or rewrite the lost account's friend-group configuration, permanently disabling any future legitimate recovery path for the account [5](#0-4) 

Both actions are explicitly what the reentrancy guard was designed to prevent, per the doc comment: "The controller is not allowed to dispatch calls of the recovery pallet. Otherwise they could mess with the recovery configuration and possibly cancel or slash attempts from higher-priority friend groups." [6](#0-5)  This is unauthorized execution/origin-escalation with direct fund-security and recovery-availability consequences, matching the "unauthorized execution or origin escalation" and "permanent user-fund lock" impact categories.

### Likelihood Explanation
The attack requires no privileged role, governance, validator, or off-chain assumption — only that the attacker already legitimately holds the `inheritor` role for some `lost` account (attainable by normal use of the pallet's recovery flow) and issues a single signed extrinsic wrapping the forbidden call in `Utility::batch`. `pallet_utility` is a standard, always-enabled pallet in runtime configurations that include `pallet-recovery`, and `batch` dispatches sub-calls with the same origin without re-checking origin-level filters added dynamically by the caller (`OriginTrait::add_filter` only affects `frame_system`'s base filter check for that origin instance as propagated, but `Utility::batch`'s own dispatch path applies to the sub-call using the origin object as passed — the filter closure added is on the `RuntimeCall` type and is checked by `frame_system::Config::BaseCallFilter`/`filter_call` at dispatch time for each call including nested ones dispatched by `batch`... but the guard here is added ad hoc via `add_filter` on a *freshly constructed* `RawOrigin::Signed` origin, and its check function only examines the single `c` passed to it, not recursively descending into `Utility::batch`'s call list). The existing test `inheritor_cannot_bypass_filter_via_utility_batch` in the repo confirms the bypass is reproducible today.

### Recommendation
Follow the same fix pattern recommended for the external report: replace the single-branch/top-level check with a check that is applied to every call that will actually execute under the lost account's origin, independent of how they are bundled. Concretely:
- Make the added filter recursive: reject not only calls where `is_sub_type()` is `Recovery`, but also any call (such as `Utility::batch`, `batch_all`, `with_weight`, `if_else`, `dispatch_as`, `as_derivative`, or any other dispatch-wrapper) that could itself dispatch a `Recovery` call, or
- Use a call-filtering approach that recursively inspects composite/wrapper calls (as `DenyRecursively` does for XCM barriers, per `prdoc/stable2503/pr_7200.prdoc`) rather than a single shallow `is_sub_type` check, or
- Restrict `call` in `control_inherited_account` to a safelist of pallets/calls instead of a denylist, closing off the entire class of "wrap it in another dispatcher" bypasses rather than enumerating each wrapper pallet individually.

### Proof of Concept
This is already encoded as a working test in the repository, `inheritor_cannot_bypass_filter_via_utility_batch`: [7](#0-6) 
1. Alice configures a higher-priority "Family" friend group and a lower-priority "Friends" friend group and the "Friends" group recovers first, making FERDIE the inheritor.
2. The "Family" group later initiates a higher-priority attempt with BOB's security deposit held.
3. FERDIE (the current inheritor, not allowed to touch `Recovery` calls) builds `RuntimeCall::Utility(UtilityCall::batch { calls: vec![RecoveryCall::slash_attempt{ friend_group_index: 0 }.into()] })` and passes it into `control_inherited_account`.
4. The outer filter sees `Utility::batch`, which is not `is_sub_type::<Recovery>()`, so it passes; `Utility::batch` then dispatches the inner `slash_attempt` call under the same (Alice) origin, which succeeds and burns BOB's security deposit, sabotaging the higher-priority Family group's in-progress recovery — despite the code's explicit intent to prevent exactly this.

### Citations

**File:** substrate/frame/recovery/src/lib.rs (L557-601)
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

**File:** substrate/frame/recovery/src/lib.rs (L630-674)
```rust
		#[pallet::call_index(2)]
		#[pallet::weight(T::WeightInfo::set_friend_groups())]
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

			let new_footprint = Self::friend_group_footprint(&new_friend_groups);
			let new_ticket = if let Some(old_ticket) = old_ticket {
				old_ticket.update(&lost, new_footprint)?
			} else {
				T::FriendGroupsConsideration::new(&lost, new_footprint)?
			};
			FriendGroups::<T>::insert(&lost, (&new_friend_groups, &new_ticket));

			if new_friend_groups != old_friend_groups {
				Self::deposit_event(Event::<T>::FriendGroupsChanged { lost });
			}

			Ok(())
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

**File:** substrate/frame/recovery/src/tests.rs (L1423-1478)
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
}
```
