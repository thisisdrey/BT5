The code matches the claim exactly as cited: `utilize()` unconditionally removes the region from storage before any `Workplan` capacity check, and both `do_assign` and `do_pool` silently drop the `try_push` failure via `.is_ok()` while still emitting the success event and returning `Ok(())`.All the code I've reviewed confirms the claim's factual assertions precisely as cited.

Audit Report

## Title
Region consumed and `Event::Assigned`/`Event::Pooled` emitted even when `Workplan` insertion silently fails, permanently burning a paid-for Coretime region - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

## Summary
`utilize()` in `substrate/frame/broker/src/utility_impls.rs` unconditionally removes the Region from `Regions` storage before any check on `Workplan` capacity is performed. `do_assign` and `do_pool` in `substrate/frame/broker/src/dispatchable_impls.rs` then attempt `workplan.try_push(...)` but only act on success via `.is_ok()`; on failure the write is silently skipped while `Event::Assigned`/`Event::Pooled` is still deposited and `Ok(())` is still returned, giving false success confirmation while the consumed Region's coretime is never scheduled.

## Finding Description
`utilize()` at [1](#0-0)  unconditionally executes `Regions::<T>::remove(&region_id)`, destroying the caller's Region record regardless of downstream outcomes. Both `do_assign` at [2](#0-1)  and `do_pool` at [3](#0-2)  guard the `Workplan::<T>::insert` write with `if workplan.try_push(...).is_ok() { ... }`, meaning a bounded-vec overflow silently drops the entire settlement effect (the `ScheduleItem`, and in `do_pool`'s case also the `InstaPoolIo`/`InstaPoolContribution` updates) without surfacing any error. Both functions then unconditionally call `Self::deposit_event(Event::Assigned{...})`/`Event::Pooled{...}` at lines 379 and 414 respectively, and return `Ok(())`.

`Workplan` is typed as `Schedule = BoundedVec<ScheduleItem, ConstU32<{ CORE_MASK_BITS as u32 }>>` (80 entries) at [4](#0-3) . `do_assign` retains only non-overlapping mask items before pushing (line 340), but `do_pool` performs no such retain before its `try_push` at line 402-404, so overlapping pool contributions can accumulate items in the same `(begin, core)` key even faster, making the 80-entry cap reachable purely through repeated unprivileged calls to the public `interlace`, `assign`, and `pool` extrinsics exposed at [5](#0-4) .

## Impact Explanation
This matches the "message queues... payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot and the "permanent user-fund lock" impact gate: the paid-for Region NFT is unconditionally destroyed by `Regions::<T>::remove` in `utilize()`, but the corresponding on-chain scheduling effect (`Workplan` entry, and pool accounting in `do_pool`) can silently fail to be written. The caller receives `Ok(())` and a success event despite having permanently lost their purchased coretime allocation with no workload ever scheduled and no refund mechanism.

## Likelihood Explanation
Reaching the 80-entry `Workplan` bound at a single `(begin, core)` key requires accumulating that many `ScheduleItem`s there, which is achievable via repeated legitimate use of the public, unprivileged `interlace`, `assign`, and `pool` extrinsics (e.g., splitting a region into many single-bit mask fragments and assigning/pooling each, or multiple independent region owners targeting the same core/timeslice), especially exploitable through `do_pool` since it lacks the overlap-retention step `do_assign` has. No malicious peer, validator, governance action, or off-repo control is required.

## Recommendation
In `do_assign` and `do_pool`, propagate the `try_push` failure as a hard error (e.g., `Error::<T>::WorkplanFull`) instead of silently ignoring it via `.is_ok()`, so the whole extrinsic — including the region consumption performed inside `utilize()` — aborts atomically when the `Workplan` write cannot succeed. This keeps region consumption and settlement atomic and prevents the false-success event from being emitted.

## Proof of Concept
1. Populate the `Workplan` for a specific `(begin, core)` key with 80 `ScheduleItem`s via repeated unprivileged calls to `interlace` + `assign`/`pool` (or overlapping `pool` calls, which skip the retain check) targeting that key.
2. A region owner holding a `RegionId` matching that now-full `(begin, core)` key calls the public `assign` (or `pool`) extrinsic.
3. `utilize()` unconditionally removes their `Region` from `Regions` storage.
4. `workplan.try_push(...)` returns `Err` (bounded vec full); `.is_ok()` is `false`, so `Workplan::<T>::insert` (and in `do_pool`, `InstaPoolIo`/`InstaPoolContribution` updates) are skipped.
5. `Event::Assigned`/`Event::Pooled` is still deposited and the extrinsic returns `Ok(())`, confirming success to the caller while their coretime region is permanently lost and never scheduled.

### Citations

**File:** substrate/frame/broker/src/utility_impls.rs (L107-119)
```rust
	pub(crate) fn utilize(
		mut region_id: RegionId,
		maybe_check_owner: Option<T::AccountId>,
		finality: Finality,
	) -> Result<Option<(RegionId, RegionRecordOf<T>)>, Error<T>> {
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let region = Regions::<T>::get(&region_id).ok_or(Error::<T>::UnknownRegion)?;

		if let Some(check_owner) = maybe_check_owner {
			ensure!(Some(check_owner) == region.owner, Error::<T>::NotOwner);
		}

		Regions::<T>::remove(&region_id);
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L330-349)
```rust
		if let Some((region_id, region)) = Self::utilize(region_id, maybe_check_owner, finality)? {
			let workplan_key = (region_id.begin, region_id.core);
			let mut workplan = Workplan::<T>::get(&workplan_key).unwrap_or_default();

			// Remove this region from the pool in case it has been assigned provisionally. If we
			// get this far then it is still in `Regions` and thus could only have been pooled
			// provisionally.
			Self::force_unpool_region(region_id, &region, &status);

			// Ensure no previous allocations exist.
			workplan.retain(|i| (i.mask & region_id.mask).is_void());
			if workplan
				.try_push(ScheduleItem {
					mask: region_id.mask,
					assignment: CoreAssignment::Task(target),
				})
				.is_ok()
			{
				Workplan::<T>::insert(&workplan_key, &workplan);
			}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L398-412)
```rust
		if let Some((region_id, region)) = Self::utilize(region_id, maybe_check_owner, finality)? {
			let workplan_key = (region_id.begin, region_id.core);
			let mut workplan = Workplan::<T>::get(&workplan_key).unwrap_or_default();
			let duration = region.end.saturating_sub(region_id.begin);
			if workplan
				.try_push(ScheduleItem { mask: region_id.mask, assignment: CoreAssignment::Pool })
				.is_ok()
			{
				Workplan::<T>::insert(&workplan_key, &workplan);
				let size = region_id.mask.count_ones() as i32;
				InstaPoolIo::<T>::mutate(region_id.begin, |a| a.private.saturating_accrue(size));
				InstaPoolIo::<T>::mutate(region.end, |a| a.private.saturating_reduce(size));
				let record = ContributionRecord { length: duration, payee };
				InstaPoolContribution::<T>::insert(&region_id, record);
			}
```

**File:** substrate/frame/broker/src/types.rs (L76-86)
```rust
/// An distinct item which can be scheduled on a Polkadot Core.
#[derive(
	Encode, Decode, DecodeWithMemTracking, Clone, PartialEq, Eq, Debug, TypeInfo, MaxEncodedLen,
)]
pub struct ScheduleItem {
	/// The regularity parts in which this Item will be scheduled on the Core.
	pub mask: CoreMask,
	/// The job that the Core should be doing.
	pub assignment: CoreAssignment,
}
pub type Schedule = BoundedVec<ScheduleItem, ConstU32<{ CORE_MASK_BITS as u32 }>>;
```

**File:** substrate/frame/broker/src/lib.rs (L781-790)
```rust
		#[pallet::call_index(9)]
		pub fn interlace(
			origin: OriginFor<T>,
			region_id: RegionId,
			pivot: CoreMask,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			Self::do_interlace(region_id, Some(who), pivot)?;
			Ok(())
		}
```
