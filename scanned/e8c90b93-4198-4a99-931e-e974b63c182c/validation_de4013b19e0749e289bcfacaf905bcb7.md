Based on my research, I found a strong structural analog to the described bug class in `pallet-scheduler`'s weight-accounting logic.

### Title
Scheduler silently discards weight-overrun signal in `execute_dispatch`, letting per-block weight budget be under-accounted - (File: `substrate/frame/scheduler/src/lib.rs`)

### Summary
`Pallet::<T>::execute_dispatch` pre-checks that a scheduled call fits the remaining `WeightMeter` budget using the *declared* worst-case weight, then dispatches the call, and finally tries to charge the meter with the *actual* post-dispatch weight. The two `try_consume` calls that record the real consumption are wrapped in `let _ = ...`, discarding the `Err` result entirely.

### Finding Description
In `execute_dispatch` [1](#0-0) , the pre-dispatch check only validates `base_weight + call_weight` (the statically declared dispatch weight) against the meter via `can_consume`. After the call runs, the *actual* weight (`maybe_actual_call_weight` from `PostDispatchInfo`, falling back to the declared weight) is charged with:
```
let _ = weight.try_consume(base_weight);
let _ = weight.try_consume(call_weight);
```
`WeightMeter::try_consume` [2](#0-1)  only mutates `consumed` on success; on failure it returns `Err(())` and leaves `consumed` untouched. Because the caller discards this `Err` with `let _ =`, whenever the actual post-dispatch weight reported by a dispatchable exceeds what was pre-checked, the excess is never accounted for in the meter — the "budget left" is not reduced to reflect the real overage, exactly mirroring the reported OVM bug where `EXCEEDS_NUISANCE_GAS` was raised but never propagated into the accounted consumption.

This under-accounted meter is then reused by the callers `service_task` [3](#0-2)  and `service_agenda`/`service_agendas` [4](#0-3)  to decide whether there is still enough weight to process further scheduled tasks in the same block (`weight.can_consume(base_weight)` at line 1301, and `weight.can_consume(service_agenda_base_weight)` at line 1245). Since the meter's `consumed` value can be silently smaller than the true weight actually spent by the runtime, the loop can keep dispatching additional scheduled tasks past the intended per-block weight limit.

### Impact Explanation
If the meter under-reports true consumption, `on_initialize` weight accounting for the block can exceed the configured `MaximumSchedulerWeight`/block weight limit while the pallet still believes there is budget left, causing more scheduled work to execute than the runtime intended for that block. This falls under "public underpriced work that degrades block production" in the impact scope, since scheduled tasks (including those submitted by unprivileged signed origins where permissionless scheduling is configured) can cause the executed weight to silently exceed the accounted/charged weight for the block.

### Likelihood Explanation
The path is reachable through the completely public `service_agendas` hook that runs every block via `on_initialize`, requiring no privileged actor, governance, validator, or malicious peer — only a scheduled call whose post-dispatch `actual_weight` diverges upward from its pre-dispatch declared weight. This can occur for any dispatchable in the runtime whose `WeightInfo`/post-info accounting slightly underestimates pre-dispatch and slightly overestimates post-dispatch, a class of implementation bug that is plausible across dozens of pallets and not something callers of the scheduler can control or prevent.

### Recommendation
Do not discard the result of `try_consume` for actual post-dispatch weight in `execute_dispatch`. If the actual weight would exceed the meter's remaining capacity, saturate the meter to its limit (e.g., call an explicit "consume all remaining" operation) rather than ignoring the error, so that subsequent scheduling logic correctly observes an exhausted budget.

### Proof of Concept
1. Configure a dispatchable `Call::X` whose `#[weight]` annotation under-declares pre-dispatch weight relative to what `PostDispatchInfo::actual_weight` it reports (a benchmarking/weight-info inconsistency, reachable in principle by any pallet's dispatch logic).
2. Schedule several instances of `Call::X` via `pallet_scheduler::schedule` for the same block.
3. During `on_initialize`, `execute_dispatch` will `can_consume` — pass on the low declared weight, dispatch the call, and then attempt `try_consume(actual_weight)`, which fails silently via `let _ =` when `actual_weight` is larger.
4. `weight.consumed()` remains lower than the true weight spent; `service_agenda`'s `can_consume` checks therefore keep permitting further tasks to run in the same block, exceeding the intended weight budget. [1](#0-0) [2](#0-1)

### Citations

**File:** substrate/frame/scheduler/src/lib.rs (L1233-1329)
```rust
	fn service_agendas(weight: &mut WeightMeter, now: BlockNumberFor<T>, max: u32) {
		if weight.try_consume(T::WeightInfo::service_agendas_base()).is_err() {
			return;
		}

		let mut incomplete_since = now + One::one();
		let mut when = IncompleteSince::<T>::take().unwrap_or(now);
		let mut is_first = true; // first task from the first agenda.

		let max_items = T::MaxScheduledPerBlock::get();
		let mut count_down = max;
		let service_agenda_base_weight = T::WeightInfo::service_agenda_base(max_items);
		while count_down > 0 && when <= now && weight.can_consume(service_agenda_base_weight) {
			if !Self::service_agenda(weight, is_first, now, when, u32::MAX) {
				incomplete_since = incomplete_since.min(when);
			}
			is_first = false;
			when.saturating_inc();
			count_down.saturating_dec();
		}
		incomplete_since = incomplete_since.min(when);
		if incomplete_since <= now {
			Self::deposit_event(Event::AgendaIncomplete { when: incomplete_since });
			IncompleteSince::<T>::put(incomplete_since);
		} else {
			// The next scheduler iteration should typically start from `now + 1` (`next_iter_now`).
			// However, if the [`Config::BlockNumberProvider`] is not a local block number provider,
			// then `next_iter_now` could be `now + n` where `n > 1`. In this case, we want to start
			// from `now + 1` to ensure we don't miss any agendas.
			IncompleteSince::<T>::put(now + One::one());
		}
	}

	/// Returns `true` if the agenda was fully completed, `false` if it should be revisited at a
	/// later block.
	fn service_agenda(
		weight: &mut WeightMeter,
		mut is_first: bool,
		now: BlockNumberFor<T>,
		when: BlockNumberFor<T>,
		max: u32,
	) -> bool {
		let mut agenda = Agenda::<T>::get(when);
		let mut ordered = agenda
			.iter()
			.enumerate()
			.filter_map(|(index, maybe_item)| {
				maybe_item.as_ref().map(|item| (index as u32, item.priority))
			})
			.collect::<Vec<_>>();
		ordered.sort_by_key(|k| k.1);
		let within_limit = weight
			.try_consume(T::WeightInfo::service_agenda_base(ordered.len() as u32))
			.is_ok();
		debug_assert!(within_limit, "weight limit should have been checked in advance");

		// Items which we know can be executed and have postponed for execution in a later block.
		let mut postponed = (ordered.len() as u32).saturating_sub(max);
		// Items which we don't know can ever be executed.
		let mut dropped = 0;

		for (agenda_index, _) in ordered.into_iter().take(max as usize) {
			let Some(task) = agenda[agenda_index as usize].take() else { continue };
			let base_weight = T::WeightInfo::service_task(
				task.call.lookup_len().map(|x| x as usize),
				task.maybe_id.is_some(),
				task.maybe_periodic.is_some(),
			);
			if !weight.can_consume(base_weight) {
				postponed += 1;
				agenda[agenda_index as usize] = Some(task);
				break;
			}
			let result = Self::service_task(weight, now, when, agenda_index, is_first, task);
			agenda[agenda_index as usize] = match result {
				Err((Unavailable, slot)) => {
					dropped += 1;
					slot
				},
				Err((Overweight, slot)) => {
					postponed += 1;
					slot
				},
				Ok(()) => {
					is_first = false;
					None
				},
			};
		}
		if postponed > 0 || dropped > 0 {
			Agenda::<T>::insert(when, agenda);
		} else {
			Agenda::<T>::remove(when);
		}

		postponed == 0
	}
```

**File:** substrate/frame/scheduler/src/lib.rs (L1337-1377)
```rust
	fn service_task(
		weight: &mut WeightMeter,
		now: BlockNumberFor<T>,
		when: BlockNumberFor<T>,
		agenda_index: u32,
		is_first: bool,
		mut task: ScheduledOf<T>,
	) -> Result<(), (ServiceTaskError, Option<ScheduledOf<T>>)> {
		if let Some(ref id) = task.maybe_id {
			Lookup::<T>::remove(id);
		}

		let (call, lookup_len) = match T::Preimages::peek(&task.call) {
			Ok(c) => c,
			Err(_) => {
				Self::deposit_event(Event::CallUnavailable {
					task: (when, agenda_index),
					id: task.maybe_id,
				});

				// It was not available when we needed it, so we don't need to have requested it
				// anymore.
				T::Preimages::drop(&task.call);

				// We don't know why `peek` failed, thus we most account here for the "full weight".
				let _ = weight.try_consume(T::WeightInfo::service_task(
					task.call.lookup_len().map(|x| x as usize),
					task.maybe_id.is_some(),
					task.maybe_periodic.is_some(),
				));

				return Err((Unavailable, Some(task)));
			},
		};

		let _ = weight.try_consume(T::WeightInfo::service_task(
			lookup_len.map(|x| x as usize),
			task.maybe_id.is_some(),
			task.maybe_periodic.is_some(),
		));

```

**File:** substrate/frame/scheduler/src/lib.rs (L1443-1471)
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
			Ok(post_info) => (post_info.actual_weight, Ok(())),
			Err(error_and_info) => {
				(error_and_info.post_info.actual_weight, Err(error_and_info.error))
			},
		};
		let call_weight = maybe_actual_call_weight.unwrap_or(call_weight);
		let _ = weight.try_consume(base_weight);
		let _ = weight.try_consume(call_weight);
		Ok(result)
	}
```

**File:** substrate/primitives/weights/src/weight_meter.rs (L127-139)
```rust
	/// Consume the given weight after checking that it can be consumed.
	///
	/// Returns `Ok` if the weight can be consumed or otherwise an `Err`.
	pub fn try_consume(&mut self, w: Weight) -> Result<(), ()> {
		self.consumed.checked_add(&w).map_or(Err(()), |test| {
			if test.any_gt(self.limit) {
				Err(())
			} else {
				self.consumed = test;
				Ok(())
			}
		})
	}
```
