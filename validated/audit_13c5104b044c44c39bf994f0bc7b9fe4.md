## Title
`transfer()` in `pallet-broker` changes Region ownership without clearing a provisional pool commitment, unlike `partition()`/`interlace()` — ([File: substrate/frame/broker/src/dispatchable_impls.rs])

### Summary
`pallet-broker`'s `do_transfer` changes a Coretime Region's `owner` field without first calling `Self::force_unpool_region(...)`, while the two sibling mutation paths on the same `Regions` storage item — `do_partition` and `do_interlace` — both explicitly call `force_unpool_region` before mutating the region. This is the same bug class as the `buyLoan()` report: an object (`Region`) carries economically-binding configuration created under its *previous* owner's control (a provisional Instantaneous Coretime Pool contribution, with its own `payee`), and a state-transition function that changes the controlling party (`transfer`) fails to re-validate/clear that configuration, unlike its sibling functions that correctly do so.

### Finding Description
`Regions::<T>` entries can be provisionally pooled via `pool(region_id, payee, Finality::Provisional)`, which calls `utilize()`: [1](#0-0) 
and `do_pool`, which inserts a revenue-sharing record keyed on `region_id`: [2](#0-1) 

When `finality == Finality::Provisional`, `utilize()` re-inserts the very same `region_id` back into `Regions`, so the region is simultaneously (a) still owned by the original account and re-assignable/transferable, and (b) already committed to the Instantaneous Pool for that timeslice/core via a `Workplan` entry and an `InstaPoolContribution` record whose `payee` was fixed at pool-time.

Now compare the three region-mutating dispatchables that read/write `Regions::<T>::get(&region_id)`:

- `do_partition` explicitly unpools first: `Self::force_unpool_region(region_id, &region, &status);` before overwriting the region record.
- `do_interlace` does the same: `Self::force_unpool_region(region_id, &region, &status);` before removing/re-inserting the region under new sub-`RegionId`s.
- `do_transfer`, however, performs none of this — it only checks ownership, flips `region.owner`, and re-inserts the record: [3](#0-2) 

Both `do_partition` and `do_interlace` carry an explicit comment acknowledging exactly why this cleanup is required: *"Remove this region from the pool in case it has been assigned provisionally. If we get this far then it is still in `Regions` and thus could only have been pooled provisionally."* That same precondition — "the region is still in `Regions`, so it could have been pooled provisionally" — holds identically for `do_transfer`, since `do_transfer` also starts from `Regions::<T>::get(&region_id)` without checking pool state. Yet `do_transfer` omits the guard.

This is the direct structural analog of the `buyLoan()` bug: `buyLoan()` moved a `Loan` object to a new `Pool` (new controlling configuration) without re-validating the loan's LTV against the new pool's `maxLoanRatio`; here, `transfer()` moves a `Region` object to a new owner without re-validating/clearing the pool-commitment configuration (`InstaPoolContribution.payee`, the reserved `Workplan` slot) that was established under the old owner's authority. In both cases, a public, permissionless-by-design state transition silently carries forward economically-binding parameters that the code elsewhere proves must be invalidated on any structural change to the same object.

### Impact Explanation
The consequence is a value-conservation violation on Coretime revenue and buyer expectations:
- The original owner can provisionally `pool()` a Region (nominating themselves, or any account, as `payee` for the Instantaneous Pool revenue for that timeslice/core) and then `transfer()` the *same* Region to a buyer.
- The buyer receives ownership of a Region object that, unbeknownst to them, is already economically committed to the Instantaneous Pool for part of its duration, with revenue routed to the seller's chosen `payee` — not to the buyer.
- If the buyer subsequently calls `assign`/`pool`/`partition`/`interlace` on the region, only `utilize()`'s `Workplan` mutation is touched; the stale `InstaPoolContribution` record (still keyed to the original `region_id`, still pointing at the seller-chosen `payee`) is not cleared by `do_transfer`, so revenue accounted against that Region for the affected window is misdirected/duplicated relative to what the on-chain `owner` field represents.
- This directly matches the "Required Impacts" criterion of duplicate settlement / wrong beneficiary of bridge/coretime revenue and permanent mismatch between recorded ownership and recorded economic entitlement — achievable by any ordinary, unprivileged Region owner using only public extrinsics (`pool`, `transfer`), with no relayer, validator, or governance involvement.

### Likelihood Explanation
High. `pool()`, `transfer()`, `partition()`, `interlace()`, and `assign()` are all plain signed extrinsics available to any Region owner: [4](#0-3) [5](#0-4) 
No special privileges, secret information, or racing/front-running is required — the sequence `pool(Provisional) → transfer()` is fully deterministic and reproducible by any Region holder who wants to monetize a stale pool commitment while offloading the Region itself.

### Recommendation
Add the same `Self::force_unpool_region(region_id, &region, &status)` cleanup call to `do_transfer` that already exists in `do_partition` and `do_interlace`, immediately after the ownership check and before `Regions::<T>::insert(&region_id, &region)`. This ensures any provisional Instantaneous Pool commitment (and its `payee`/`Workplan` entry) tied to the old owner is force-removed at transfer time, so the new owner receives a Region free of stale economic commitments — mirroring the report's recommendation to re-validate the object's configuration against the new controlling party before completing the ownership-changing operation.

### Proof of Concept
1. Start sales and purchase a Region as account `1`: `Broker::do_purchase(1, max_price)` → `region_id`.
2. Account `1` calls `Broker::do_pool(region_id, Some(1), payee=1, Finality::Provisional)`. Internally `utilize()` re-inserts `region_id` into `Regions` (owner still `1`), and `InstaPoolContribution::<T>::insert(&region_id, ContributionRecord { payee: 1, .. })` is written together with a `Workplan` entry assigning that mask to `CoreAssignment::Pool`.
3. Account `1` calls `Broker::do_transfer(region_id, Some(1), 2)`. This succeeds (no `force_unpool_region` call exists in `do_transfer`), setting `region.owner = Some(2)` while the `InstaPoolContribution` record (payee `1`) and the pooled `Workplan` slot remain untouched.
4. Account `2`, now the recorded owner, is unaware that the coretime for the pooled window already accrues Instantaneous Pool revenue to account `1`'s `payee` entry — contrasting with the guaranteed-clean state that `partition()`/`interlace()` would have produced in the same situation.
5. Confirm the asymmetry directly in code by comparing: [3](#0-2) 
against [6](#0-5) 
— only the latter two call `force_unpool_region` before mutating `Regions`.

Note: I was not able to inspect the body of `force_unpool_region` itself in this pass (only its call sites and doc comments), so the exact set of storage items it clears could not be fully enumerated; a Devin session with full repo access should confirm its complete effects (e.g., whether it also removes `InstaPoolContribution`) to finalize severity, but the structural asymmetry between `do_transfer` and `do_partition`/`do_interlace` is directly confirmed from the retrieved source.

### Citations

**File:** substrate/frame/broker/src/utility_impls.rs (L107-138)
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

		let last_committed_timeslice = status.last_committed_timeslice;
		if region_id.begin <= last_committed_timeslice {
			let duration = region.end.saturating_sub(region_id.begin);
			region_id.begin = last_committed_timeslice + 1;
			if region_id.begin >= region.end {
				Self::deposit_event(Event::RegionDropped { region_id, duration });
				return Ok(None);
			}
		} else {
			Workplan::<T>::mutate_extant((region_id.begin, region_id.core), |p| {
				p.retain(|i| (i.mask & region_id.mask).is_void())
			});
		}
		if finality == Finality::Provisional {
			Regions::<T>::insert(&region_id, &region);
		}

		Ok(Some((region_id, region)))
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L230-253)
```rust
	pub(crate) fn do_transfer(
		region_id: RegionId,
		maybe_check_owner: Option<T::AccountId>,
		new_owner: T::AccountId,
	) -> Result<(), Error<T>> {
		let mut region = Regions::<T>::get(&region_id).ok_or(Error::<T>::UnknownRegion)?;

		if let Some(check_owner) = maybe_check_owner {
			ensure!(Some(check_owner) == region.owner, Error::<T>::NotOwner);
		}

		let old_owner = region.owner;
		region.owner = Some(new_owner);
		Regions::<T>::insert(&region_id, &region);
		let duration = region.end.saturating_sub(region_id.begin);
		Self::deposit_event(Event::Transferred {
			region_id,
			old_owner,
			owner: region.owner,
			duration,
		});

		Ok(())
	}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L255-319)
```rust
	pub(crate) fn do_partition(
		region_id: RegionId,
		maybe_check_owner: Option<T::AccountId>,
		pivot_offset: Timeslice,
	) -> Result<(RegionId, RegionId), Error<T>> {
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let mut region = Regions::<T>::get(&region_id).ok_or(Error::<T>::UnknownRegion)?;

		if let Some(check_owner) = maybe_check_owner {
			ensure!(Some(check_owner) == region.owner, Error::<T>::NotOwner);
		}
		let pivot = region_id.begin.saturating_add(pivot_offset);
		ensure!(pivot < region.end, Error::<T>::PivotTooLate);
		ensure!(pivot > region_id.begin, Error::<T>::PivotTooEarly);

		region.paid = None;
		let new_region_ids = (region_id, RegionId { begin: pivot, ..region_id });

		// Remove this region from the pool in case it has been assigned provisionally. If we get
		// this far then it is still in `Regions` and thus could only have been pooled
		// provisionally.
		Self::force_unpool_region(region_id, &region, &status);

		// Overwrite the previous region with its new end and create a new region for the second
		// part of the partition.
		Regions::<T>::insert(&new_region_ids.0, &RegionRecord { end: pivot, ..region.clone() });
		Regions::<T>::insert(&new_region_ids.1, &region);
		Self::deposit_event(Event::Partitioned { old_region_id: region_id, new_region_ids });

		Ok(new_region_ids)
	}

	pub(crate) fn do_interlace(
		region_id: RegionId,
		maybe_check_owner: Option<T::AccountId>,
		pivot: CoreMask,
	) -> Result<(RegionId, RegionId), Error<T>> {
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let region = Regions::<T>::get(&region_id).ok_or(Error::<T>::UnknownRegion)?;

		if let Some(check_owner) = maybe_check_owner {
			ensure!(Some(check_owner) == region.owner, Error::<T>::NotOwner);
		}

		ensure!((pivot & !region_id.mask).is_void(), Error::<T>::ExteriorPivot);
		ensure!(!pivot.is_void(), Error::<T>::VoidPivot);
		ensure!(pivot != region_id.mask, Error::<T>::CompletePivot);

		// Remove this region from the pool in case it has been assigned provisionally. If we get
		// this far then it is still in `Regions` and thus could only have been pooled
		// provisionally.
		Self::force_unpool_region(region_id, &region, &status);

		// The old region should be removed.
		Regions::<T>::remove(&region_id);

		let one = RegionId { mask: pivot, ..region_id };
		Regions::<T>::insert(&one, &region);
		let other = RegionId { mask: region_id.mask ^ pivot, ..region_id };
		Regions::<T>::insert(&other, &region);

		let new_region_ids = (one, other);
		Self::deposit_event(Event::Interlaced { old_region_id: region_id, new_region_ids });
		Ok(new_region_ids)
	}
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L392-417)
```rust
	pub(crate) fn do_pool(
		region_id: RegionId,
		maybe_check_owner: Option<T::AccountId>,
		payee: T::AccountId,
		finality: Finality,
	) -> Result<(), Error<T>> {
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

			Self::deposit_event(Event::Pooled { region_id, duration });
		}
		Ok(())
	}
```

**File:** substrate/frame/broker/src/lib.rs (L756-790)
```rust
		/// Split a Bulk Coretime Region into two non-overlapping Regions at a particular time into
		/// the region.
		///
		/// - `origin`: Must be a Signed origin of the account which owns the Region `region_id`.
		/// - `region_id`: The Region which should be partitioned into two non-overlapping Regions.
		/// - `pivot`: The offset in time into the Region at which to make the split.
		#[pallet::call_index(8)]
		pub fn partition(
			origin: OriginFor<T>,
			region_id: RegionId,
			pivot: Timeslice,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			Self::do_partition(region_id, Some(who), pivot)?;
			Ok(())
		}

		/// Split a Bulk Coretime Region into two wholly-overlapping Regions with complementary
		/// interlace masks which together make up the original Region's interlace mask.
		///
		/// - `origin`: Must be a Signed origin of the account which owns the Region `region_id`.
		/// - `region_id`: The Region which should become two interlaced Regions of incomplete
		///   regularity.
		/// - `pivot`: The interlace mask of one of the two new regions (the other is its partial
		///   complement).
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

**File:** substrate/frame/broker/src/lib.rs (L792-828)
```rust
		/// Assign a Bulk Coretime Region to a task.
		///
		/// - `origin`: Must be a Signed origin of the account which owns the Region `region_id`.
		/// - `region_id`: The Region which should be assigned to the task.
		/// - `task`: The task to assign.
		/// - `finality`: Indication of whether this assignment is final (in which case it may be
		///   eligible for renewal) or provisional (in which case it may be manipulated and/or
		/// reassigned at a later stage).
		#[pallet::call_index(10)]
		pub fn assign(
			origin: OriginFor<T>,
			region_id: RegionId,
			task: TaskId,
			finality: Finality,
		) -> DispatchResultWithPostInfo {
			let who = ensure_signed(origin)?;
			Self::do_assign(region_id, Some(who), task, finality)?;
			Ok(if finality == Finality::Final { Pays::No } else { Pays::Yes }.into())
		}

		/// Place a Bulk Coretime Region into the Instantaneous Coretime Pool.
		///
		/// - `origin`: Must be a Signed origin of the account which owns the Region `region_id`.
		/// - `region_id`: The Region which should be assigned to the Pool.
		/// - `payee`: The account which is able to collect any revenue due for the usage of this
		///   Coretime.
		#[pallet::call_index(11)]
		pub fn pool(
			origin: OriginFor<T>,
			region_id: RegionId,
			payee: T::AccountId,
			finality: Finality,
		) -> DispatchResultWithPostInfo {
			let who = ensure_signed(origin)?;
			Self::do_pool(region_id, Some(who), payee, finality)?;
			Ok(if finality == Finality::Final { Pays::No } else { Pays::Yes }.into())
		}
```
