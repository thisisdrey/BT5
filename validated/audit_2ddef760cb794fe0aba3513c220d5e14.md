Audit Report

## Title
`pallet-recovery::control_inherited_account`'s reentrancy filter is dropped when the wrapped call is deferred through `pallet-scheduler`, allowing the inheritor to execute forbidden Recovery calls against the recovered account - (File: `substrate/frame/recovery/src/lib.rs`)

## Summary
`control_inherited_account` dispatches the caller-supplied call under a `recovered`-account origin that has an `add_filter` closure blocking any nested `pallet-recovery` call, but that filter lives only on the transient `RuntimeOrigin` object and is never persisted. If the wrapped call is `Scheduler::schedule`/`schedule_named` carrying a forbidden `pallet-recovery` call (e.g. `slash_attempt`, `set_friend_groups`) as its payload, the scheduler stores only the bare `PalletsOrigin` for `recovered` and, when it later dispatches, reconstructs a fresh origin with no memory of the filter, letting the inheritor execute the exact calls the reentrancy guard exists to block.

## Finding Description
`control_inherited_account` builds a `recovered`-signed origin and installs a one-shot filter that blocks any call that is a sub-type of `pallet-recovery`'s own `Call` enum: [1](#0-0) . This filter is added via `OriginTrait::add_filter`, which per the trait definition mutates the in-memory `RuntimeOrigin` value only [2](#0-1) .

`pallet-scheduler::schedule`/`schedule_named` do not dispatch the supplied call immediately; they store `origin.caller().clone()` — a bare `T::PalletsOrigin`, which carries no trace of any filter attached to the `RuntimeOrigin` wrapper — together with the call, for later execution [3](#0-2) . At the scheduled block, `execute_dispatch` reconstructs a fresh `RuntimeOrigin` from that stored `PalletsOrigin` and dispatches the payload call against it [4](#0-3) , and the pallet's own documentation explicitly states that any filter added via a mechanism like proxy will not be honored when the scheduled call executes [5](#0-4) .

Exploit path:
1. Inheritor `FERDIE` (a real, on-chain `Inheritor` for `recovered`) calls `control_inherited_account(recovered, Box::new(Scheduler::schedule_named(id, when, None, priority, Box::new(RecoveryCall::slash_attempt{..}))))`.
2. `Scheduler::schedule_named` is not itself a `pallet-recovery` call, so it passes the reentrancy filter and dispatches successfully under the filtered `recovered` origin, storing only `PalletsOrigin::Signed(recovered)` plus the boxed `slash_attempt` call.
3. At block `when`, the scheduler reconstructs a filter-free origin for `recovered` and dispatches `slash_attempt` directly — bypassing the exact guard that `control_inherited_account`'s comment says exists to prevent the inheritor from messing with recovery configuration or slashing higher-priority attempts [6](#0-5) .

This is the same class of gap the existing regression tests were written to catch for the `Utility::batch` vector [7](#0-6)  and for direct nested calls [8](#0-7) , but no test exercises the scheduler-deferred path, and the filter's implementation (`c.is_sub_type().is_none()`, checking only the immediate call, not payloads captured by other pallets for later redispatch) does not close it.

## Impact Explanation
If `pallet-recovery` is deployed in a runtime alongside `pallet-scheduler`, and the runtime's `BaseCallFilter` permits ordinary signed accounts to call `Scheduler::schedule`/`schedule_named`, an inheritor can defeat the reentrancy guard and execute forbidden Recovery-pallet calls (e.g., slashing a higher-priority friend group's attempt, wiping friend groups) as the recovered account — an origin-escalation/unauthorized-execution bug matching the "unauthorized execution or origin escalation" and "public wrappers must not widen origin, bypass filters" categories.

However, I was unable to confirm within this session that `pallet-recovery` is actually wired into any live runtime in this repository alongside `pallet-scheduler` with a `BaseCallFilter` that permits normal accounts to call `Scheduler::schedule`/`schedule_named`. My searches located `pallet_recovery` and `Scheduler`/`BaseCallFilter` references in `cumulus/parachains/runtimes/assets/asset-hub-westend/src/lib.rs`, but tool budget was exhausted before I could inspect those matches to determine whether both pallets are configured together and whether the filter permits this call from a normal signed origin. The recovery pallet's own mock runtime (`substrate/frame/recovery/src/mock.rs`) does not appear to include `pallet-scheduler` at all, meaning the described bypass is not directly demonstrable in the pallet's own test harness without adding scheduler to the mock — a nontrivial gap in reproducibility as currently presented.

## Likelihood Explanation
The attacker only needs to be a legitimate `Inheritor` of some `recovered` account (attainable through ordinary recovery flow) and needs the ability to call `Scheduler::schedule`/`schedule_named` from a normal signed origin — no privileged, governance, or validator access is required. The main open question is deployment: whether any runtime in this repo actually combines `pallet-recovery` and `pallet-scheduler` with a filter configuration that allows this scheduling call. Without confirming that, the claim's "reachable exploit path" element is only partially demonstrated.

## Recommendation
- In `control_inherited_account`, broaden the reentrancy filter to also block scheduling primitives (`pallet-scheduler::schedule`, `schedule_named`, `schedule_after`, `schedule_named_after`) and any other "capture origin now, dispatch later" call, mirroring how `pallet-utility::batch_all` blocks nested `batch_all`.
- More generally, have `pallet-scheduler::execute_dispatch` persist and re-apply origin-level filters that were active when `schedule`/`schedule_named` was invoked, rather than silently discarding them, if such filters are meant to constrain the full transitive call graph rather than just the immediate call.
- Add a regression test analogous to `inheritor_cannot_bypass_filter_via_utility_batch` that wires `pallet-scheduler` into the recovery pallet's mock runtime and verifies the inheritor cannot use `Scheduler::schedule_named` to defer execution of a forbidden `pallet-recovery` call.

## Proof of Concept
1. Extend `substrate/frame/recovery/src/mock.rs` to include `pallet-scheduler` in the mock `Runtime`, with a `BaseCallFilter` that permits `Scheduler::schedule_named` from a signed origin.
2. Reproduce the setup from `inheritor_can_slash_higher_priority_attempts_and_remove_friend_groups` (`substrate/frame/recovery/src/tests.rs:1353-1420`): configure two friend groups so `FERDIE` becomes inheritor while a higher-priority `BOB`-led attempt is ongoing.
3. Instead of calling `slash_attempt` directly, have `FERDIE` call `Recovery::control_inherited_account(signed(FERDIE), ALICE, Box::new(Scheduler::schedule_named(id, when, None, 0, Box::new(RecoveryCall::slash_attempt{ friend_group_index: 0 }))))`.
4. Advance the mock chain to block `when` and run `on_initialize`/scheduler service logic.
5. Assert `BOB`'s security deposit was slashed despite the reentrancy filter — this should fail (i.e., demonstrate the bypass) where the equivalent `Utility::batch` test in `substrate/frame/recovery/src/tests.rs:1423-1477` currently passes.

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

**File:** substrate/frame/support/src/traits/dispatch.rs (L488-489)
```rust
	/// Add a filter to the origin.
	fn add_filter(&mut self, filter: impl Fn(&Self::Call) -> bool + 'static);
```

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

```
