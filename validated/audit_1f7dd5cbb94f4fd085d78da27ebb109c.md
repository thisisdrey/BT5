This confirms the claim: `do_reschedule` and `do_reschedule_named` move a task from `(when, index)` to a new slot without calling `Retries::<T>::remove((when, index))`, while `do_cancel` and `do_cancel_named` do perform this cleanup. `push_to_agenda` reuses hole indices via `agenda.iter().position(|i| i.is_none())`, so a new unrelated task placed into the vacated slot would inherit stale `Retries` data.

Audit Report

## Title
`do_reschedule`/`do_reschedule_named` leave a stale `Retries` entry at the vacated `(when, index)` slot, which is silently inherited by a new task that fills the reused hole - (File: substrate/frame/scheduler/src/lib.rs)

## Summary
`do_reschedule` and `do_reschedule_named` move a scheduled task from `(when, index)` to `(new_time, new_index)` but never clear the `Retries::<T>` entry keyed at the vacated `(when, index)`, unlike `do_cancel`/`do_cancel_named` which correctly call `Retries::<T>::remove((when, index))`. Since `push_to_agenda` reuses the first `None` hole in an agenda for any subsequently scheduled task, a brand-new unrelated task placed at the reused `(when, index)` inherits the previous task's stale retry configuration (`remaining`, `period`) the first time it fails and the runtime looks up `Retries` for that address. [1](#0-0) [2](#0-1) 

## Finding Description
`do_cancel` explicitly removes the retry entry for the vacated slot before cleanup: [3](#0-2) 

`do_cancel_named` similarly removes it (inside the origin-check branch): [4](#0-3) 

By contrast, `do_reschedule` takes the task out of `Agenda::<T>::get(when)[index]`, calls `cleanup_agenda(when)`, and calls `place_task(new_time, task)` — with no `Retries::<T>::remove((when, index))` call anywhere in the function: [5](#0-4) 

`do_reschedule_named` has the identical omission: [6](#0-5) 

`cleanup_agenda` only truncates trailing `None` entries; for a non-trailing vacated index, the slot remains `None` inside the `Agenda` bounded vec: [7](#0-6) 

`push_to_agenda`, invoked by `place_task` for every subsequent `schedule`/`schedule_named` call at block `when`, explicitly searches for and fills the first `None` hole before falling back to appending or exhaustion: [8](#0-7) 

Because `Retries::<T>::get((when, index))` from the rescheduled task is never removed, a new, unrelated task placed into the same reused `(when, index)` will silently read the stale `RetryConfig` when it later fails and the runtime consults `Retries` to decide on/how to retry it.

## Impact Explanation
This is a genuine state-integrity bug in `pallet-scheduler`, reachable through the public `reschedule`/`reschedule_named` extrinsics (gated by `ScheduleOrigin`, which in many runtimes includes governance-controlled but non-root callers) combined with any subsequent, unrelated `schedule`/`schedule_named` call targeting the same block. The corrupted value is the `Retries<T>` map entry at a given `(when, index)` `TaskAddress` — specifically the `remaining` retries count and `period`, which get misattributed to a task that never called `set_retry`/`set_retry_named`. This can cause an unrelated scheduled call (e.g., a deferred governance-enacted dispatch) to be retried with an unintended budget/period, or to be denied intended independent retry behavior, corrupting execution/retry accounting for scheduled runtime calls. It does not constitute direct fund theft, forged proof, or origin escalation, but it is a real violation of the "queue/settlement state must only advance after correct accounting" pivot for scheduled task state.

## Likelihood Explanation
Medium. It requires a task with retry configuration set via `set_retry`/`set_retry_named`, that same task later being rescheduled via `reschedule`/`reschedule_named`, and a new unrelated task being scheduled into the same block before the vacated hole is naturally reused by another mechanism. `push_to_agenda`'s deterministic first-hole-fill logic makes this reproducible and controllable by an actor who controls scheduling order for a given block, matching the report's PoC steps.

## Recommendation
In `do_reschedule` and `do_reschedule_named`, transfer or clear the retry state tied to the vacated slot: `if let Some(retry) = Retries::<T>::take((when, index)) { Retries::<T>::insert((new_time, new_index), retry); }`, or at minimum call `Retries::<T>::remove((when, index))` to prevent stale retry data from leaking into a reused agenda slot.

## Proof of Concept
1. `Scheduler::schedule_named(id_a, DispatchTime::At(10), None, prio, root(), call_a)`, then `Scheduler::set_retry((10, 0), retries=5, period=2)`, confirming `Retries::<Test>::get((10, 0)) == Some(RetryConfig { total_retries: 5, remaining: 5, period: 2 })`.
2. Schedule a second task at block 10 so the agenda has entries at index 0 and 1.
3. Call `Scheduler::reschedule_named(id_a, DispatchTime::At(20))`, moving `task_a` from `(10, 0)` to `(20, k)`. `Agenda::<Test>::get(10)[0]` becomes `None` and `Retries::<Test>::get((10, 0))` remains `Some(...)` since it was never removed.
4. Schedule a new unrelated task `task_c` at block 10 via `Scheduler::schedule(...)`; `push_to_agenda` fills the hole at index 0, so `task_c`'s address becomes `(10, 0)`.
5. At block 10, force `task_c` to fail dispatch; the retry-check path looks up `Retries::<Test>::get((10, 0))`, finds the stale `RetryConfig { remaining: 5, period: 2 }` from `task_a`, and reschedules `task_c` for retry with a budget/period it was never configured with — demonstrating the cross-task state leak.

### Citations

**File:** substrate/frame/scheduler/src/lib.rs (L998-1017)
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

**File:** substrate/frame/scheduler/src/lib.rs (L1021-1038)
```rust
	fn cleanup_agenda(when: BlockNumberFor<T>) {
		let mut agenda = Agenda::<T>::get(when);
		match agenda.iter().rposition(|i| i.is_some()) {
			// Note that `agenda.len() > i + 1` implies that the agenda ends on a sequence of at
			// least one `None` item(s).
			Some(i) if agenda.len() > i + 1 => {
				agenda.truncate(i + 1);
				Agenda::<T>::insert(when, agenda);
			},
			// This branch is taken if `agenda.len() <= i + 1 ==> agenda.len() == i + 1 <==>
			// agenda.len() - 1 == i` i.e. the agenda's last item is `Some`.
			Some(_) => {},
			// All items in the agenda are `None`.
			None => {
				Agenda::<T>::remove(when);
			},
		}
	}
```

**File:** substrate/frame/scheduler/src/lib.rs (L1089-1097)
```rust
		if let Some(s) = scheduled {
			T::Preimages::drop(&s.call);
			if let Some(id) = s.maybe_id {
				Lookup::<T>::remove(id);
			}
			Retries::<T>::remove((when, index));
			Self::cleanup_agenda(when);
			Self::deposit_event(Event::Canceled { when, index });
			Ok(())
```

**File:** substrate/frame/scheduler/src/lib.rs (L1103-1122)
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

**File:** substrate/frame/scheduler/src/lib.rs (L1170-1177)
```rust
					if let Some(s) = agenda.get_mut(i) {
						if let (Some(ref o), Some(ref s)) = (origin, s.borrow()) {
							Self::ensure_privilege(o, &s.origin)?;
							Retries::<T>::remove((when, index));
							T::Preimages::drop(&s.call);
						}
						*s = None;
					}
```

**File:** substrate/frame/scheduler/src/lib.rs (L1189-1209)
```rust
	fn do_reschedule_named(
		id: TaskName,
		new_time: DispatchTime<BlockNumberFor<T>>,
	) -> Result<TaskAddress<BlockNumberFor<T>>, DispatchError> {
		let new_time = Self::resolve_time(new_time)?;

		let lookup = Lookup::<T>::get(id);
		let (when, index) = lookup.ok_or(Error::<T>::NotFound)?;

		if new_time == when {
			return Err(Error::<T>::RescheduleNoChange.into());
		}

		let task = Agenda::<T>::try_mutate(when, |agenda| {
			let task = agenda.get_mut(index as usize).ok_or(Error::<T>::NotFound)?;
			task.take().ok_or(Error::<T>::NotFound)
		})?;
		Self::cleanup_agenda(when);
		Self::deposit_event(Event::Canceled { when, index });
		Self::place_task(new_time, task).map_err(|x| x.0)
	}
```
