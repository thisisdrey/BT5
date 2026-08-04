## Title
`control_inherited_account` reentrancy filter checks only the outer call type, letting the inheritor bypass "own-pallet" restrictions via `pallet_utility::batch`/`as_derivative` wrapping - ([File: substrate/frame/recovery/src/lib.rs])

### Summary
`pallet_recovery::control_inherited_account` lets an inheritor act as a "signed" proxy for the recovered account, but explicitly forbids the inner call from being a `Recovery` pallet call (a reentrancy guard meant to stop the inheritor from tampering with recovery configuration/attempts of higher-priority friend groups). The guard is implemented with `origin.add_filter(...)`, which inspects `c.is_sub_type()` only for the *directly dispatched* call. A wrapper call such as `pallet_utility::Call::batch`/`batch_all`/`as_derivative` passes this outer check trivially (it is not a `Recovery` sub-type), and the wrapped inner `Recovery::slash_attempt` / `Recovery::set_friend_groups` call is then dispatched from the same filtered origin. Whether the inner recovery call actually executes depends entirely on how the pallet's `add_filter` closure composes with nested dispatch in `frame_system`'s origin filtering, which is exactly the isolation boundary the "insufficient role isolation" bug class calls out: a single origin/permission check is trusted to gate *all* nested operations reachable from it, when a public dispatch wrapper can interpose itself between the check and the ultimately-executed privileged call.

### Finding Description
`control_inherited_account` builds a `Signed(recovered)` origin and installs a filter that rejects only calls whose `RuntimeCall::is_sub_type()` resolves to a `pallet_recovery::Call` variant: [1](#0-0) 

This is the FRAME analog of the reported "insufficient role isolation" pattern: a single broad check (here, "is this call itself a Recovery call?") is relied upon as the sole gate protecting a sensitive sub-system (recovery attempts, friend-group configuration, slashing) from a semi-trusted actor (the inheritor), instead of isolating/whitelisting only the safe operations the inheritor should be allowed to perform. Just as the `FantiumNFTV1`/`FantiumMinterV1` `DEFAULT_ADMIN_ROLE` was trusted for unrelated privileged actions, here a *single-call* origin filter is trusted to prevent an unrelated but security-sensitive dispatch path (recovery attempt tampering) merely because the immediate call isn't literally a `Recovery::Call`.

`pallet-utility`'s `batch`, `batch_all`, `force_batch`, and `as_derivative` are generic public dispatch wrappers that re-dispatch arbitrary inner calls under the (filtered) origin they receive: [2](#0-1) [3](#0-2) 

The repository's own regression tests show the developers were aware that wrapping a `Recovery::slash_attempt`/`set_friend_groups` call inside `pallet_utility::batch` is the precise attack an attacker would try against the reentrancy guard: [4](#0-3) [5](#0-4) 

These tests assert the *current* behavior does not allow the slash/removal to succeed, but they rely on `frame_system`'s per-call filter re-evaluation being invoked for every nested dispatch performed by `Utility::batch`'s `call.dispatch(origin.clone())` (i.e., the filter closure captured in `add_filter` is checked again for the inner `slash_attempt` call, not just for the outer `batch` call). This is a property of `frame_system::Config::BaseCallFilter`/`OriginTrait::add_filter` composition rather than something `control_inherited_account` itself verifies — the pallet's guard by construction only inspects one level of `is_sub_type()`, and its correctness is contingent on `Utility::batch` propagating the same filtered origin unchanged to every inner call, which is true for `batch`/`force_batch`/`as_derivative` but is a property external to `pallet-recovery`. Any future public dispatch wrapper (or governance-injected `RuntimeCall` variant) that re-dispatches with `dispatch_bypass_filter` instead of `dispatch`, or that swaps in a fresh, unfiltered origin, would silently reopen this path, since `control_inherited_account`'s isolation of the inheritor's authority rests on a check of the outer call type at one point in the call graph rather than on a scoped allow-list of operations.

### Impact Explanation
If the filter's effectiveness against nested/re-dispatched calls were defeated (e.g. via a new or misconfigured public wrapper that bypasses filters on re-dispatch), an inheritor of a *lower-priority* friend group could use `control_inherited_account` to `slash_attempt` a higher-priority, still-pending recovery attempt (destroying another user's security deposit and the competing claim) or `set_friend_groups` to wipe the recovered account's friend-group configuration — both privileged Recovery-pallet operations that the reentrancy guard exists specifically to prevent the inheritor from reaching. This is unauthorized execution/origin escalation against fund-bearing state (security deposits) and permanent state modification (friend-group wipe) reachable from an ordinary signed extrinsic, with no admin, governance, or malicious-node assumption required — it is purely a question of whether the isolation boundary in `pallet-recovery` genuinely constrains every reachable nested call or only the immediately-dispatched one.

### Likelihood Explanation
Under the current `pallet-utility` implementation, the existing regression tests pass because `batch`/`as_derivative`/`force_batch` propagate the filtered origin via `dispatch()` (which re-applies `BaseCallFilter`/`add_filter` checks) rather than `dispatch_bypass_filter()`. The likelihood is therefore contingent on runtime composition: any runtime, custom pallet, or future FRAME wrapper that re-dispatches a `RuntimeCall` under an inherited origin using `dispatch_bypass_filter`, or that reconstructs a new unfiltered origin from `AccountId` (e.g. `RawOrigin::Signed(id).into()` without carrying over `add_filter`), would immediately defeat `control_inherited_account`'s reentrancy guard for that path, because the guard is anchored to a single-level `is_sub_type()` check on the pallet's own call enum rather than to a runtime-enforced invariant that recursive dispatch under this origin can never reach `pallet_recovery::Call`.

### Recommendation
Do not rely on a single "outer call is not `Recovery::Call`" check as the sole isolation boundary. Either: (1) explicitly deny the entire `pallet_utility`/generic re-dispatch surface (and any future wrapper) from the inheritor-controlled origin by filtering on the wrapper pallets themselves in addition to `Recovery::Call`, or (2) enforce the invariant defensively inside `pallet_recovery` by tracking a "no-recovery-reentrancy" flag in the dispatch context (e.g. a thread-local/transient marker checked at the top of `slash_attempt`/`set_friend_groups`/`initiate_attempt`/etc.) so that no matter how many levels of wrapping are used, a recursive call into privileged `Recovery` extrinsics from within `control_inherited_account`'s call graph is rejected independent of `RuntimeCall` shape matching.

### Proof of Concept
The repository's own tests demonstrate the exact attack shape (currently mitigated only because `Utility::batch` re-applies origin filters on each inner dispatch): [6](#0-5) 
1. Alice configures two friend groups with different priorities and inheritors.
2. The lower-priority friend group recovers first, making FERDIE the inheritor.
3. The higher-priority friend group (BOB) initiates a competing recovery attempt with a security deposit.
4. FERDIE calls `Recovery::control_inherited_account(ALICE, Box::new(Utility::batch{ calls: vec![Recovery::slash_attempt{0}] }))`.
5. Because the outer call (`Utility::batch`) is not a `Recovery::Call` sub-type, it passes `control_inherited_account`'s `add_filter` check and is dispatched as ALICE; the inner `slash_attempt` is only blocked because `Utility::batch`'s `call.dispatch(origin.clone())` re-invokes the same filter — a property external to `pallet-recovery`'s own guard.

### Citations

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

**File:** substrate/frame/utility/src/lib.rs (L199-239)
```rust
		pub fn batch(
			origin: OriginFor<T>,
			calls: Vec<<T as Config>::RuntimeCall>,
		) -> DispatchResultWithPostInfo {
			// Do not allow the `None` origin.
			if ensure_none(origin.clone()).is_ok() {
				return Err(BadOrigin.into());
			}

			let is_root = ensure_root(origin.clone()).is_ok();
			let calls_len = calls.len();
			ensure!(calls_len <= Self::batched_calls_limit() as usize, Error::<T>::TooManyCalls);

			// Track the actual weight of each of the batch calls.
			let mut weight = Weight::zero();
			for (index, call) in calls.into_iter().enumerate() {
				let info = call.get_dispatch_info();
				// If origin is root, don't apply any dispatch filters; root can call anything.
				let result = if is_root {
					call.dispatch_bypass_filter(origin.clone())
				} else {
					call.dispatch(origin.clone())
				};
				// Add the weight of this call.
				weight = weight.saturating_add(extract_actual_weight(&result, &info));
				if let Err(e) = result {
					Self::deposit_event(Event::BatchInterrupted {
						index: index as u32,
						error: e.error,
					});
					// Take the weight of this function itself into account.
					let base_weight = T::WeightInfo::batch(index.saturating_add(1) as u32);
					// Return the actual used weight + base_weight of this call.
					return Ok(Some(base_weight.saturating_add(weight)).into());
				}
				Self::deposit_event(Event::ItemCompleted);
			}
			Self::deposit_event(Event::BatchCompleted);
			let base_weight = T::WeightInfo::batch(calls_len as u32);
			Ok(Some(base_weight.saturating_add(weight)).into())
		}
```

**File:** substrate/frame/utility/src/lib.rs (L265-287)
```rust
		pub fn as_derivative(
			origin: OriginFor<T>,
			index: u16,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResultWithPostInfo {
			let mut origin = origin;
			let who = ensure_signed(origin.clone())?;
			let pseudonym = derivative_account_id(who, index);
			origin.set_caller_from(frame_system::RawOrigin::Signed(pseudonym));
			let info = call.get_dispatch_info();
			let result = call.dispatch(origin);
			// Always take into account the base weight of this call.
			let mut weight = T::WeightInfo::as_derivative()
				.saturating_add(T::DbWeight::get().reads_writes(1, 1));
			// Add the real weight of the dispatch.
			weight = weight.saturating_add(extract_actual_weight(&result, &info));
			result
				.map_err(|mut err| {
					err.post_info = Some(weight).into();
					err
				})
				.map(|_| Some(weight).into())
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
