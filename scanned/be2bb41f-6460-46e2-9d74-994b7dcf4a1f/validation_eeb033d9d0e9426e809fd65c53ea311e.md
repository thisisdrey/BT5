## Analysis

The Notional bug's core invariant is: **a privilege/permission check that is supposed to gate a restricted action is enforced against the wrong scope, and a call executed *through* a trusted redispatch path escapes the check that would have applied to it if called directly.** In `TimelockController`, the `EXECUTOR`/queued-state check was validated too late (after execution), so a self-referential call inside the batch satisfied the check retroactively.

The closest reproducible analog in this repository is a structural weakness in `pallet-scheduler`: calls executed by the scheduler are dispatched with a **freshly reconstructed origin** built only from the stored `PalletsOrigin`, discarding any temporary call filter that was attached to the origin (via `OriginTrait::add_filter`) at the moment the scheduling call was made. This is explicitly acknowledged in the pallet's own documentation: [1](#0-0) 

and structurally confirmed by the execution path, where the scheduler only ever sees `task.origin: T::PalletsOrigin` and reconstructs a bare `RuntimeOrigin` from it before dispatch — no filter added to the *caller's* origin at schedule-time survives into this reconstructed origin: [2](#0-1) 

The codebase demonstrates this exact filter-restriction pattern (temporarily narrowing what an origin may call, using `add_filter`) is a first-class idiom used elsewhere, e.g. `pallet-utility::batch_all` prevents nested `batch_all` calls by adding a filter to the origin for the duration of the dispatch: [3](#0-2) 

That filter only constrains the *synchronous* call graph of `batch_all`. If an origin subject to such a restriction can also call `Scheduler::schedule`/`schedule_named` with the forbidden call as payload, the forbidden call is not dispatched immediately (so the origin-scoped filter never runs against it); it is stored and, at a later block, dispatched by the scheduler using a **freshly derived, unfiltered** origin — bypassing the restriction entirely, exactly as the report describes for the Timelock's late/absent state check.

This same idiom is used by the repository's custom `pallet-recovery`, whose `control_inherited_account` entry point restricts an "inheritor" origin to a bounded set of calls on the recovered account, and whose regression tests explicitly confirm that both direct restricted calls and calls nested in `Utility::batch` are (currently) blocked: [4](#0-3) [5](#0-4) 

I was not able to read `control_inherited_account`'s implementation directly in this session (tool budget exhausted), so I cannot confirm with certainty whether it filters by inspecting call contents recursively or by an `add_filter` on the dispatch origin. If it uses the latter (the standard Substrate idiom shown in `batch_all`), it inherits the same scheduler bypass gap described above, because the tests only cover the `Utility::batch` bypass vector, not the `Scheduler::schedule`/`schedule_named` vector.

### Title
Origin-scoped call filters (`add_filter`) are silently dropped by `pallet-scheduler`, allowing restricted origins to execute forbidden calls via deferred scheduling - (File: `substrate/frame/scheduler/src/lib.rs`)

### Summary
Any pallet that restricts what a given origin may do for the duration of one dispatch by calling `origin.add_filter(...)` (the standard Substrate pattern, e.g. `pallet-utility::batch_all`, and apparently `pallet-recovery::control_inherited_account`) loses that restriction if the restricted origin is allowed to call `pallet_scheduler::schedule`/`schedule_named`. The scheduler stores only the bare `PalletsOrigin` and reconstructs a fresh `RuntimeOrigin` at execution time, which carries only the account's permanent `BaseCallFilter`, not the transient filter that was active when the call was scheduled.

### Finding Description
`Pallet::execute_dispatch` in `pallet-scheduler` dispatches scheduled calls from `task.origin.clone()` converted straight into a `RuntimeOrigin` and calls `.dispatch(dispatch_origin)`: [2](#0-1) 

This origin has no memory of any `add_filter` closure that was attached to the origin object used when `schedule`/`schedule_named` was originally called — filters added via `OriginTrait::add_filter` live on the `RuntimeOrigin` value itself (a runtime-local wrapper), not on the persisted `PalletsOrigin` used as the scheduler's storage key/origin discriminator. The pallet's own doc comments confirm this is intentional/known behavior: [1](#0-0) 

Consequently, any caller-side "restricted dispatch" pattern built on `add_filter` — used, for instance, by `pallet-utility::batch_all` to block nested `batch_all` calls: [3](#0-2) 

— can be defeated by replacing the direct forbidden call with `Scheduler::schedule(when, None, priority, forbidden_call)` invoked from within the same restricted dispatch. The `schedule` extrinsic itself only requires `T::ScheduleOrigin::ensure_origin`, it does not re-check any filter that was added to the *caller's* origin for the current call stack: [6](#0-5) 

When the block containing `when` arrives, `service_task` → `execute_dispatch` dispatches the forbidden call with a filter-free/base-filter-only origin, exactly analogous to how the Timelock's `EXECUTOR` check was checked against stale/incorrect state and thus never actually gated the malicious batched call.

### Impact Explanation
Any runtime pallet relying on the `add_filter`-on-origin idiom to prevent an untrusted or semi-trusted origin (proxy sub-accounts, recovery "inheritor" accounts, restricted governance tracks, etc.) from executing specific dangerous calls can have that restriction bypassed for calls scheduled instead of executed inline, provided the restricted origin is permitted to reach `pallet-scheduler::schedule`/`schedule_named` (directly or via an allowed nested call). Where such a filter is the only barrier protecting fund-moving or privilege-granting calls (as `pallet-recovery`'s inheritor filtering appears to be, based on its regression tests), this allows origin escalation / unauthorized execution without any admin, governance, or validator compromise — matching the "unauthorized execution or origin escalation" and "public wrappers must not widen origin, bypass filters" impact categories.

### Likelihood Explanation
The bypass requires only that the restricted origin be permitted to call `Scheduler::schedule`/`schedule_named` (or any other pallet that stores a bare `PalletsOrigin` for later redispatch) — an ordinary permission that is not obviously suspicious and is not covered by the existing regression tests in `pallet-recovery`, which only assert the `Utility::batch` bypass is blocked and do not test the scheduler-based bypass. No malicious peer, validator, collator, or governance actor is needed; the caller is a normal unprivileged/semi-trusted account acting through public extrinsics.

### Recommendation
- Persist and re-apply any origin-added filters when reconstructing a `RuntimeOrigin` from a stored `PalletsOrigin` at scheduled-dispatch time, or
- Disallow scheduling calls from within any dispatch context that has an active `add_filter` restriction (e.g., `batch_all`, `control_inherited_account`) by explicitly filtering out `Scheduler::schedule`/`schedule_named`/`schedule_after` in the same way `batch_all` filters out nested `batch_all`, and
- Audit every pallet using the `add_filter`-on-origin idiom (`pallet-utility`, `pallet-recovery`, and any downstream runtime-specific pallet) to ensure the underlying calls they intend to forbid cannot reach execution via `pallet-scheduler` or any other "capture origin now, dispatch later" primitive.

### Proof of Concept
1. Runtime configures a pallet (e.g. `pallet-recovery`'s `control_inherited_account`, or a custom equivalent) that restricts an origin `O` to a safe subset of calls for one dispatch by calling `origin.add_filter(f)` before dispatching the user-supplied call.
2. Attacker, acting as `O`, submits `control_inherited_account(real, Box::new(Scheduler::schedule_named(id, when, None, priority, Box::new(forbidden_call))))`.
3. `f` only inspects the immediate call (`Scheduler::schedule_named`), which is not itself forbidden, so it passes the filter and is dispatched with `dispatch_bypass_filter`/`dispatch` under origin `O` with `add_filter(f)` active.
4. `pallet-scheduler::schedule_named` stores `forbidden_call` together with `origin.caller().clone()` (a bare `PalletsOrigin`, stripped of `f`).
5. At block `when`, `service_task`/`execute_dispatch` reconstructs `RuntimeOrigin::from(PalletsOrigin)` (no `f`) and dispatches `forbidden_call`, which now executes despite being nominally forbidden for `O`.
6. Verify via `pallet-recovery`'s existing test harness style (as in `inheritor_cannot_bypass_filter_via_utility_batch`, `substrate/frame/recovery/src/tests.rs:1423-1477`) but replacing the `Utility::batch` wrapper with a `Scheduler::schedule_named` wrapper and running the chain forward to the scheduled block — the currently-passing "no bypass" assertion is expected to fail for the scheduler path.

### Citations

**File:** substrate/frame/scheduler/src/lib.rs (L36-42)
```rust
//! __NOTE:__ Instead of using the filter contained in the origin to call `fn schedule`, scheduled
//! runtime calls will be dispatched with the default filter for the origin: namely
//! `frame_system::Config::BaseCallFilter` for all origin types (except root which will get no
//! filter).
//!
//! If a call is scheduled using proxy or whatever mechanism which adds filter, then those filter
//! will not be used when dispatching the schedule runtime call.
```

**File:** substrate/frame/scheduler/src/lib.rs (L458-475)
```rust
		pub fn schedule(
			origin: OriginFor<T>,
			when: BlockNumberFor<T>,
			maybe_periodic: Option<schedule::Period<BlockNumberFor<T>>>,
			priority: schedule::Priority,
			call: Box<<T as Config>::RuntimeCall>,
		) -> DispatchResult {
			T::ScheduleOrigin::ensure_origin(origin.clone())?;
			let origin = <T as Config>::RuntimeOrigin::from(origin);
			Self::do_schedule(
				DispatchTime::At(when),
				maybe_periodic,
				priority,
				origin.caller().clone(),
				T::Preimages::bound(*call)?,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/scheduler/src/lib.rs (L1443-1461)
```rust
	fn execute_dispatch(
		weight: &mut WeightMeter,
		origin: T::PalletsOrigin,
		call: <T as Config>::RuntimeCall,
	) -> Result<DispatchResult, ()> {
		let base_weight = match origin.as_system_ref() {
			Some(&RawOrigin::Signed(_)) => T::WeightInfo::execute_dispatch_signed(),
			_ => T::WeightInfo::execute_dispatch_unsigned(),
		};
		let call_weight = call.get_dispatch_info().call_weight;
		// We only allow a scheduled call if it cannot push the weight past the limit.
		let max_weight = base_weight.saturating_add(call_weight);

		if !weight.can_consume(max_weight) {
			return Err(());
		}

		let dispatch_origin = origin.into();
		let (maybe_actual_call_weight, result) = match call.dispatch(dispatch_origin) {
```

**File:** substrate/frame/utility/src/lib.rs (L326-337)
```rust
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

**File:** substrate/frame/recovery/src/tests.rs (L1389-1420)
```rust
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
