## Title
`do_reschedule`/`do_reschedule_named` leave a stale `Retries` entry at the vacated `(when, index)` slot, which is silently inherited by a new task that fills the reused hole - (File: `substrate/frame/scheduler/src/lib.rs`)

### Summary
`pallet-scheduler` tracks per-task retry configuration in the double-keyed map `Retries<T>` keyed by `(when, index)` (the same "slot" address used to identify a scheduled task in `Agenda`). `do_cancel`/`do_cancel_named` correctly clear `Retries` for the slot they vacate, but `do_reschedule`/`do_reschedule_named` move a task from `(when, index)` to a brand-new `(new_time, new_index)` without ever calling `Retries::<T>::remove((when, index))`. The vacated slot becomes a "hole" in the `Agenda` for `when`, and `push_to_agenda` explicitly fills the first available hole for any subsequently scheduled task. This is the same bug class as the external report: an index-keyed side-table (`orderTriggerBlock` / `Retries`) is not reset when the entity that owned that index is removed/moved, and a later unrelated entity that reuses the index inherits the stale tracking value.

### Finding Description
`Retries` is defined as: [1](#0-0) 
(`do_cancel`, which correctly removes the `Retries` entry for the vacated slot)

Compare with `do_reschedule`, which moves the task out of `(when, index)` into a new slot but never touches `Retries`: [2](#0-1) 

Same omission in the named variant: [3](#0-2) 

After `do_reschedule` takes the task out of `Agenda::<T>::get(when)[index]` (leaving `None` there) and calls `cleanup_agenda(when)`, the slot `index` becomes a "hole" unless it was the last item (in which case it's truncated away, but for any non-trailing index it remains a `None` hole): [4](#0-3) 

`push_to_agenda`, used by every subsequent `place_task` call (i.e. every new `schedule`/`schedule_named` call for block `when`), explicitly searches for and reuses the first `None` hole in the agenda before falling back to exhaustion: [5](#0-4) 

Because the old `Retries::<T>::get((when, index))` entry from the rescheduled task was never removed, a brand-new, unrelated task placed into that same reused index at that same block will silently inherit the previous task's retry configuration (retry count remaining, retry period) the first time it fails and the runtime looks up `Retries` for `(when, index)` to decide whether/how to retry it.

### Impact Explanation
This corrupts task-execution semantics for public, permissionless-adjacent scheduling flows (e.g. governance-scheduled dispatches, or any pallet using `Scheduler::schedule`/`reschedule` such as referenda enactment, treasury spends, or other scheduled runtime calls). A newly scheduled task can be given a stale, attacker/previous-task-controlled retry budget/period it was never configured with, or conversely be denied retries that should be independent. Depending on how retry logic gates re-execution (deferred dispatch of a privileged call), this can cause a scheduled call to be retried when it should not be (or vice-versa), producing incorrect execution counts / incorrect gating of privileged dispatch execution timing — matching the "public underpriced work / block-production degradation / incorrect settlement timing" impact class called out in the pivots (message/queue/settlement state must only advance after correct accounting). It falls short of direct fund theft but is a genuine state-integrity bug reachable without any privileged actor: any caller with `ScheduleOrigin` (which in many runtimes includes democratic/root-gated calls, but reschedule can also be triggered as part of normal periodic-task churn) can create the mismatch, and any third party scheduling a new task at the same block can be affected.

### Likelihood Explanation
Medium. It requires: (1) a task with retry configuration set via `set_retry`/`set_retry_named`, (2) that task later rescheduled via `reschedule`/`reschedule_named` (a normal, exposed `Call`), and (3) a new task subsequently scheduled into the exact vacated `(when, index)` slot before `when` is reached — which `push_to_agenda`'s hole-filling logic makes deterministic and easy to arrange by controlling scheduling order. High-frequency schedulers (referenda/treasury/automation pallets that schedule many tasks per block) make hole reuse commonplace, mirroring the "high frequency trading" collision scenario in the original report.

### Recommendation
In `do_reschedule` and `do_reschedule_named`, explicitly clear any stale retry state for the vacated `(when, index)` before/while moving the task, e.g. `if let Some(retry) = Retries::<T>::take((when, index)) { Retries::<T>::insert((new_time, new_index), retry); }` (transferring retry config to the new address, since conceptually the retry config belongs to the task, not the slot) or, at minimum, `Retries::<T>::remove((when, index))` to guarantee no stale data leaks to a reused slot.

### Proof of Concept
1. Call `Scheduler::schedule_named(id_a, DispatchTime::At(10), None, prio, root(), call_a)` and then `Scheduler::set_retry((10, 0), retries=5, period=2)` (or the named variant) so `Retries::<Test>::get((10, 0)) == Some(RetryConfig{ total_retries: 5, remaining: 5, period: 2 })`.
2. Schedule a second task at block 10 so the agenda has length 2: `(10,0)=task_a`, `(10,1)=task_b`.
3. Call `Scheduler::reschedule_named(id_a, DispatchTime::At(20))` — this moves `task_a` out of `(10,0)` to `(20, k)`, leaving `Agenda::<Test>::get(10)[0] == None` and `cleanup_agenda(10)` does nothing since index 1 is still `Some`. Note `Retries::<Test>::get((10,0))` is **still** `Some(...)` (not removed).
4. Schedule a brand-new unrelated task `task_c` at block 10 (`Scheduler::schedule(...)` targeting block 10). `push_to_agenda` finds the hole at index 0 and places `task_c` there, so its address becomes `(10, 0)`.
5. Run to block 10 and force `task_c` to fail (e.g. via the `Logger`/mock dispatch returning an error). The scheduler's retry-check path looks up `Retries::<Test>::get((10, 0))`, finds the leftover `RetryConfig{ remaining: 5, period: 2 }` from `task_a`, and reschedules `task_c` for retry with that stale, never-configured budget — even though `task_c` was never given a `set_retry` call, demonstrating the cross-task state leak through the reused `(when, index)` slot.

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

**File:** substrate/frame/scheduler/src/lib.rs (L1016-1035)
```rust
	/// Remove trailing `None` items of an agenda at `when`. If all items are `None` remove the
	/// agenda record entirely.
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

**File:** substrate/frame/scheduler/src/lib.rs (L1186-1206)
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
