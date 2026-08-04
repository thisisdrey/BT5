## Title
`do_reschedule` leaves a stale `Retries` entry keyed by `(when, index)`, which is silently inherited by an unrelated task that later fills the same agenda slot - (File: `substrate/frame/scheduler/src/lib.rs`)

### Summary
`pallet-scheduler`'s `do_reschedule` moves a task from its original `(when, index)` slot to a new time, but — unlike `do_cancel` and `do_cancel_named` — it never clears the `Retries` storage entry associated with the old `(when, index)` address. Because freed agenda slots are reused ("fills holes") by `push_to_agenda`, a completely unrelated, later-scheduled task can land at the same `(when, index)` and silently inherit a stranger's retry configuration, causing it to be auto-rescheduled/retried on failure without the caller (or the task owner) ever requesting that behavior.

### Finding Description
The external report's core broken invariant is: a keeper/executor uses a **stale identifier** (BTC `marketId`) to route execution, while combining it with **freshly mutated state** (the updated `ETH` order's size) that was never validated to belong to that identifier — because the executing function never re-checks that the identifier still matches the current owner of the mutable record.

The scheduler exhibits the same class of bug. `Retries<T>` is keyed purely by the *positional* address `(when, index)`: [1](#0-0) 

`do_reschedule` (unnamed tasks) takes the task out of its old slot and re-places it at a new time/index, but only cleans up the agenda — it never removes `Retries::<T>::remove((when, index))`: [2](#0-1) 

Compare this with `do_cancel`, which explicitly removes the retry entry when a task is dropped: [3](#0-2) 

and `do_cancel_named`, which likewise removes `Retries::<T>::remove((when, index))`: [4](#0-3) 

Meanwhile, `push_to_agenda` — used by every scheduling path via `place_task` — deliberately fills the first `None` "hole" in an agenda `Vec` rather than always appending, as demonstrated by the pallet's own test `scheduler_v3_anon_cancel_and_schedule_fills_holes`: [5](#0-4) [6](#0-5) 

Because `do_reschedule` frees the old `(when, index)` slot (via `cleanup_agenda`) without removing the matching `Retries` entry, a subsequent, unrelated call to `schedule`/`schedule_named`/another `reschedule` that lands in the same `when` agenda can be assigned that exact freed `index` (agenda hole-filling). When `service_task` later executes that new, unrelated task and it fails, it reads: [7](#0-6) 

`Retries::<T>::take((when, agenda_index))` returns the **stale** retry configuration belonging to the old, already-relocated task, and applies it (`schedule_retry`) to the new task — exactly mirroring the external bug's pattern of combining a stale identifier with unrelated/newer mutable data during execution, because no invariant binds `Retries` to the specific task it was set for (e.g. a call hash, task id, or generation counter).

### Impact Explanation
This does not directly steal funds, but it corrupts intended runtime scheduling behavior for a public-facing subsystem used throughout governance, treasury payouts, and other pallets that rely on `pallet-scheduler` (e.g. delayed treasury spends, referenda enactment, retried XCM/bridge callbacks configured via `ScheduleOrigin`). A task that was never configured for retries can be auto-retried on failure (potentially re-executing a privileged call multiple times up to the stale `total_retries`), or a task's own bounded retry budget can be silently swapped for another task's `RetryConfig` (different `period`/`remaining`). This falls under "runtime bugs that compromise intended behavior" in the impact gate, since it produces execution outcomes (retry scheduling of privileged/root-origin calls) that were never authorized for the task that ends up receiving them.

### Likelihood Explanation
The trigger requires no privileged actor beyond whoever is already permitted to call `schedule`/`reschedule` (which, depending on runtime config, can include `ScheduleOrigin` used by pallets such as `pallet-referenda` for enactment, or root/governance-controlled call paths). The conditions are deterministic and reproducible: (1) schedule a named-with-retry-or-plain task with a `RetryConfig` set via `set_retry`, (2) `reschedule` it to a different block (freeing its `(when, index)` slot without clearing `Retries`), (3) schedule a new, unrelated task into the same original `when` agenda until it fills the freed hole at the same `index`, (4) let that new task fail during `on_initialize`. No race with block producers or external actors is needed — a single account controlling scheduling calls (or governance timing) can cause this deterministically.

### Recommendation
In `do_reschedule` (and any other path that moves/removes a task from an existing `(when, index)` slot without going through `do_cancel`), explicitly call `Retries::<T>::remove((when, index))` before `cleanup_agenda`, mirroring the cleanup already performed in `do_cancel` and `do_cancel_named`. More robustly, bind `RetryConfig` to the task's identity (e.g. keyed by task hash/id rather than positional `(when, index)`), so that slot reuse cannot cause cross-task inheritance of retry state.

### Proof of Concept
Conceptual reproduction using existing scheduler test harness (`substrate/frame/scheduler/src/tests.rs`):
1. `Scheduler::do_schedule(DispatchTime::At(4), None, 127, root(), call_a)` → address `(4, 0)`.
2. `Scheduler::set_retry((4, 0), retries: 3, period: 1)` → `Retries::<T>::get((4, 0))` is `Some`.
3. `Scheduler::do_reschedule((4, 0), DispatchTime::At(5))` → task moves to `(5, 0)`; `Agenda::<T>::get(4)` now has a `None` hole at index `0`; `Retries::<T>::get((4, 0))` is still `Some` (not removed).
4. Schedule enough additional tasks at `when = 4` to fill earlier slots, then schedule one more unrelated task `call_b` at `when = 4` — per `push_to_agenda`'s hole-filling logic it is placed at index `0`, i.e. address `(4, 0)`.
5. Run to block `4`; make `call_b` fail (e.g. via the `Logger` mock with an intentionally failing call). `service_task` calls `Retries::<T>::take((4, 0))`, retrieving the retry config originally set for `call_a`, and schedules a retry for `call_b` even though `set_retry` was never called for it.

This demonstrates that the stale `(when, index)`-keyed `Retries` entry from the rescheduled task is silently applied to an unrelated task occupying the reused slot — the runtime-scheduler analog of the reported keeper/`marketId` mismatch.

### Citations

**File:** substrate/frame/scheduler/src/lib.rs (L995-1014)
```rust
	fn push_to_agenda(
		when: BlockNumberFor<T>,
		what: ScheduledOf<T>,
	) -> Result<u32, (DispatchError, ScheduledOf<T>)> {
		let mut agenda = Agenda::<T>::get(when);
		let index = if (agenda.len() as u32) < T::MaxScheduledPerBlock::get() {
			// will always succeed due to the above check.
			let _ = agenda.try_push(Some(what));
			agenda.len() as u32 - 1
		} else {
			if let Some(hole_index) = agenda.iter().position(|i| i.is_none()) {
				agenda[hole_index] = Some(what);
				hole_index as u32
			} else {
				return Err((DispatchError::Exhausted, what));
			}
		};
		Agenda::<T>::insert(when, agenda);
		Ok(index)
	}
```

**File:** substrate/frame/scheduler/src/lib.rs (L1071-1098)
```rust
	fn do_cancel(
		origin: Option<T::PalletsOrigin>,
		(when, index): TaskAddress<BlockNumberFor<T>>,
	) -> Result<(), DispatchError> {
		let scheduled = Agenda::<T>::try_mutate(when, |agenda| {
			agenda.get_mut(index as usize).map_or(
				Ok(None),
				|s| -> Result<Option<Scheduled<_, _, _, _, _>>, DispatchError> {
					if let (Some(ref o), Some(ref s)) = (origin, s.borrow()) {
						Self::ensure_privilege(o, &s.origin)?;
					};
					Ok(s.take())
				},
			)
		})?;
		if let Some(s) = scheduled {
			T::Preimages::drop(&s.call);
			if let Some(id) = s.maybe_id {
				Lookup::<T>::remove(id);
			}
			Retries::<T>::remove((when, index));
			Self::cleanup_agenda(when);
			Self::deposit_event(Event::Canceled { when, index });
			Ok(())
		} else {
			return Err(Error::<T>::NotFound.into());
		}
	}
```

**File:** substrate/frame/scheduler/src/lib.rs (L1100-1119)
```rust
	fn do_reschedule(
		(when, index): TaskAddress<BlockNumberFor<T>>,
		new_time: DispatchTime<BlockNumberFor<T>>,
	) -> Result<TaskAddress<BlockNumberFor<T>>, DispatchError> {
		let new_time = Self::resolve_time(new_time)?;

		if new_time == when {
			return Err(Error::<T>::RescheduleNoChange.into());
		}

		let task = Agenda::<T>::try_mutate(when, |agenda| {
			let task = agenda.get_mut(index as usize).ok_or(Error::<T>::NotFound)?;
			ensure!(!matches!(task, Some(Scheduled { maybe_id: Some(_), .. })), Error::<T>::Named);
			task.take().ok_or(Error::<T>::NotFound)
		})?;
		Self::cleanup_agenda(when);
		Self::deposit_event(Event::Canceled { when, index });

		Self::place_task(new_time, task).map_err(|x| x.0)
	}
```

**File:** substrate/frame/scheduler/src/lib.rs (L1162-1184)
```rust
	fn do_cancel_named(origin: Option<T::PalletsOrigin>, id: TaskName) -> DispatchResult {
		Lookup::<T>::try_mutate_exists(id, |lookup| -> DispatchResult {
			if let Some((when, index)) = lookup.take() {
				let i = index as usize;
				Agenda::<T>::try_mutate(when, |agenda| -> DispatchResult {
					if let Some(s) = agenda.get_mut(i) {
						if let (Some(ref o), Some(ref s)) = (origin, s.borrow()) {
							Self::ensure_privilege(o, &s.origin)?;
							Retries::<T>::remove((when, index));
							T::Preimages::drop(&s.call);
						}
						*s = None;
					}
					Ok(())
				})?;
				Self::cleanup_agenda(when);
				Self::deposit_event(Event::Canceled { when, index });
				Ok(())
			} else {
				return Err(Error::<T>::NotFound.into());
			}
		})
	}
```

**File:** substrate/frame/scheduler/src/lib.rs (L1388-1402)
```rust
			Ok(result) => {
				let failed = result.is_err();
				let maybe_retry_config = Retries::<T>::take((when, agenda_index));
				Self::deposit_event(Event::Dispatched {
					task: (when, agenda_index),
					id: task.maybe_id,
					result,
				});

				match maybe_retry_config {
					Some(retry_config) if failed => {
						Self::schedule_retry(weight, now, when, agenda_index, &task, retry_config);
					},
					_ => {},
				}
```

**File:** substrate/frame/scheduler/src/tests.rs (L2660-2706)
```rust
#[test]
fn scheduler_v3_anon_cancel_and_schedule_fills_holes() {
	use frame_support::traits::schedule::v3::Anon;
	let max: u32 = <Test as Config>::MaxScheduledPerBlock::get();
	assert!(max > 3, "This test only makes sense for MaxScheduledPerBlock > 3");

	new_test_ext().execute_with(|| {
		let call =
			RuntimeCall::Logger(LoggerCall::log { i: 42, weight: Weight::from_parts(10, 0) });
		let bound = Preimage::bound(call).unwrap();
		let mut addrs = Vec::<_>::default();

		// Schedule the maximal number allowed per block.
		for _ in 0..max {
			addrs.push(
				<Scheduler as Anon<_, _, _>>::schedule(
					DispatchTime::At(4),
					None,
					127,
					root(),
					bound.clone(),
				)
				.unwrap(),
			);
		}
		// Cancel three of them.
		for addr in addrs.into_iter().take(3) {
			<Scheduler as Anon<_, _, _>>::cancel(addr).unwrap();
		}
		// Schedule three new ones.
		for i in 0..3 {
			let (_block, index) = <Scheduler as Anon<_, _, _>>::schedule(
				DispatchTime::At(4),
				None,
				127,
				root(),
				bound.clone(),
			)
			.unwrap();
			assert_eq!(i, index);
		}

		System::run_to_block::<AllPalletsWithSystem>(4);
		// Maximum number of calls are executed.
		assert_eq!(logger::log().len() as u32, max);
	});
}
```
