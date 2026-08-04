### Title
`pallet-broker::do_transfer` moves Region ownership without unpooling an active InstaPool contribution, decoupling pooled revenue entitlement from asset ownership - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

### Summary
This is a structural analog of the `LienToken#buyoutLien()` bug: an ownership-transfer path mutates who controls an asset but skips the bookkeeping step that every other mutation path on the same asset performs to keep a derived accounting structure (a "processing gate"/ledger keyed by the asset id) consistent. In `pallet-broker`, `do_partition` and `do_interlace` both explicitly call `Self::force_unpool_region(region_id, &region, &status)` before mutating a Region, precisely because a Region that has been provisionally pooled into the InstaPool carries a live `InstaPoolContribution` entry and matching `InstaPoolIo` schedule deltas that must be reversed/rescheduled whenever the Region is "redispatched." `do_transfer` — the function backing both the public `transfer` extrinsic and the privileged `force_transfer` extrinsic, as well as the NFT `Transfer` trait implementation — changes `region.owner` and re-inserts the `RegionRecord`, but never calls `force_unpool_region`. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`force_unpool_region` is the single place that reconciles the `InstaPoolContribution<T>` map and the `InstaPoolIo<T>` schedule with the fact that a Region is being taken out of circulation for its current purpose. Its own doc comment states it exists to handle regions "pooled provisionally and it is being redispatched (partition/interlace/assign)" — `transfer`/`force_transfer` is conspicuously absent from that list, and the code confirms it: `do_transfer` only reads `Regions::<T>`, checks the owner, flips `region.owner`, and re-inserts the record. [1](#0-0) 

Contrast this with `do_partition` and `do_interlace`, which both call `Self::force_unpool_region(region_id, &region, &status);` immediately after the owner check and before altering the record, in order to keep `InstaPoolContribution`/`InstaPoolIo` (the derived state) consistent with the Region's new disposition: [4](#0-3) 

`force_unpool_region` itself confirms the coupling it is meant to preserve — it `take()`s the `InstaPoolContribution` for the `region_id` and adjusts two future `InstaPoolIo` schedule points that were set when the Region was pooled, based on `region.end` and the current commitment point: [3](#0-2) 

Both the ordinary `transfer` extrinsic (owner-only) and the privileged `force_transfer` extrinsic route through the same unguarded `do_transfer`: [5](#0-4) [6](#0-5) 

The existing tests only demonstrate that `force_transfer` *succeeds* while a Region is provisionally assigned to a dedicated task — they do not exercise the InstaPool-contribution case, and none of the assertions check `InstaPoolContribution`/`InstaPoolIo` state after the transfer: [7](#0-6) 

**Exact corrupted value:** `InstaPoolContribution::<T>::get(region_id)` (payee/length) and the two `InstaPoolIo::<T>` schedule buckets it implies remain keyed to the pre-transfer contribution state after `Regions::<T>::get(region_id).owner` has already changed. Nothing in `do_transfer` re-validates or reconciles this derived state, and no other check in the pallet prevents transferring a Region while it is actively pooled.

**Why existing guards don't stop this:** the only guard in `do_transfer` is `ensure!(Some(check_owner) == region.owner, Error::<T>::NotOwner)` (skipped entirely for `force_transfer`). There is no check of `InstaPoolContribution::<T>::contains_key(region_id)` anywhere in the transfer path, unlike `do_partition`/`do_interlace`, which unconditionally call `force_unpool_region` regardless of pooled state (it is a no-op if not pooled).

### Impact Explanation
A Region that is actively pooled into the InstaPool represents a live revenue-sharing commitment: `InstaPoolContribution` records a payee and length, and `InstaPoolIo` has already scheduled compensating deltas for `region.end`. Because `do_transfer` changes legal ownership of the Region without touching this state, the pooled revenue entitlement becomes decoupled from current Region ownership with no on-chain signal to the new owner. The new owner acquires a Region that both parties may believe is fully unencumbered, but which secretly still carries a scheduled InstaPool unpooling obligation tied to the *previous* owner's contribution. If/when the new owner subsequently redispatches the Region (`assign`/`partition`/`interlace`), `force_unpool_region` fires against state that was never validated against the ownership change that occurred in between, silently reconciling `InstaPoolIo` based on a contribution the current owner did not make and may not be aware of. This is a live-scope accounting-conservation defect on bridge/coretime-adjacent pallet state (Coretime is deployed on Coretime-Westend and other coretime runtimes) — a public, unprivileged-reachable path (`transfer`) plus an admin path (`force_transfer`) both bypass the unpooling step that every other redispatch path enforces, breaking the "settle exactly once to the rightful beneficiary" invariant for pooled revenue.

### Likelihood Explanation
`transfer` is a plain signed extrinsic available to any Region owner with no additional restriction on whether the Region is currently pooled; provisional InstaPool assignment (`assign(..., Finality::Provisional)`) is also a normal owner-only action. Chaining "pool provisionally" → "transfer to counterparty" requires no privileged role, no relayer, no governance action, and no race condition — it is a two-transaction sequence fully within reach of any Region owner, matching the Sherlock report's characterization of a straightforward, unprivileged sequencing bug rather than an adversarial-infrastructure exploit.

### Recommendation
Make `do_transfer` call `Self::force_unpool_region(region_id, &region, &status)` before flipping ownership, mirroring `do_partition`/`do_interlace`, so that transferring a Region always first settles/removes any live InstaPool contribution and reconciles `InstaPoolIo` at transfer time — exactly as recommended in the source report ("add a decrease/settlement call to the transfer function before completing the ownership change").

### Proof of Concept
1. Owner A purchases/holds a Region `region_id` (`RegionRecord { owner: A, end, .. }`).
2. A calls `assign(region_id, TaskId::pool_or_similar, Finality::Provisional)`, which pools the Region — creating `InstaPoolContribution::<T>::insert(region_id, ContributionRecord { payee: A, .. })` and future `InstaPoolIo` deltas keyed off `region.end`.
3. A calls `transfer(region_id, B)` (or an `AdminOrigin` calls `force_transfer(region_id, B)`). `do_transfer` succeeds, `Regions::<T>::get(region_id).owner == B`, but `InstaPoolContribution::<T>::get(region_id)` is untouched and still references A's contribution and `InstaPoolIo` schedule.
4. Verify via test-mirroring of `force_transfer_can_transfer_provisionally_assigned_region` but with a *pooled* (InstaPool) provisional assignment instead of a dedicated-task provisional assignment, then assert `InstaPoolContribution::<T>::get(region_id).is_some()` still holds post-transfer, and that `Broker::assign`/`partition`/`interlace` by the new owner B is the first point at which `force_unpool_region` runs — reconciling state against a contribution B never made. [7](#0-6)

### Citations

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

**File:** substrate/frame/broker/src/utility_impls.rs (L141-173)
```rust
	// Remove a region from on-demand pool contributions. Useful in cases where it was pooled
	// provisionally and it is being redispatched (partition/interlace/assign).
	//
	// Takes both the region_id and (a reference to) the region as arguments to avoid another DB
	// read. No-op for regions which have not been pooled.
	pub(crate) fn force_unpool_region(
		region_id: RegionId,
		region: &RegionRecordOf<T>,
		status: &StatusRecord,
	) {
		// We don't care if this fails or not, just that it is removed if present. This is to
		// account for the case where a region is pooled provisionally and redispatched.
		if InstaPoolContribution::<T>::take(region_id).is_some() {
			// `InstaPoolHistory` is calculated from the `InstaPoolIo` one timeslice in advance.
			// Therefore we need to schedule this for the timeslice after that.
			let end_timeslice = status.last_committed_timeslice + 1;

			// InstaPoolIo has already accounted for regions that have already ended. Regions ending
			// this timeslice would have region.end == unpooled_at below.
			if region.end <= end_timeslice {
				return;
			}

			// Account for the change in `InstaPoolIo` either from the start of the region or from
			// the current timeslice if we are already part-way through the region.
			let size = region_id.mask.count_ones() as i32;
			let unpooled_at = end_timeslice.max(region_id.begin);
			InstaPoolIo::<T>::mutate(unpooled_at, |a| a.private.saturating_reduce(size));
			InstaPoolIo::<T>::mutate(region.end, |a| a.private.saturating_accrue(size));

			Self::deposit_event(Event::<T>::RegionUnpooled { region_id, when: unpooled_at });
		};
	}
```

**File:** substrate/frame/broker/src/lib.rs (L740-754)
```rust
		/// Transfer a Bulk Coretime Region to a new owner.
		///
		/// - `origin`: Must be a Signed origin of the account which owns the Region `region_id`.
		/// - `region_id`: The Region whose ownership should change.
		/// - `new_owner`: The new owner for the Region.
		#[pallet::call_index(7)]
		pub fn transfer(
			origin: OriginFor<T>,
			region_id: RegionId,
			new_owner: T::AccountId,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			Self::do_transfer(region_id, Some(who), new_owner)?;
			Ok(())
		}
```

**File:** substrate/frame/broker/src/lib.rs (L1066-1083)
```rust
		/// Transfer a Bulk Coretime Region to a new owner, ignoring the previous owner.
		///
		/// This can also be used to recover regions that have been "burned" (e.g., from an
		/// XCM reserve transfer).
		///
		/// - `origin`: Must be Root or pass `AdminOrigin`.
		/// - `region_id`: The Region whose ownership should change.
		/// - `new_owner`: The new owner for the Region.
		#[pallet::call_index(28)]
		pub fn force_transfer(
			origin: OriginFor<T>,
			region_id: RegionId,
			new_owner: T::AccountId,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin_or_root(origin)?;
			Self::do_transfer(region_id, None, new_owner)?;
			Ok(())
		}
```

**File:** substrate/frame/broker/src/tests.rs (L3120-3146)
```rust
#[test]
fn force_transfer_can_transfer_provisionally_assigned_region() {
	TestExt::new().endow(1, 1000).execute_with(|| {
		assert_ok!(Broker::do_start_sales(100, 4));
		advance_to(2);

		const OLD_OWNER: u64 = 1;
		const NEW_OWNER: u64 = 222;

		let region_id = Broker::do_purchase(OLD_OWNER, u64::max_value()).unwrap();

		assert_ok!(Broker::assign(RuntimeOrigin::signed(OLD_OWNER), region_id, 1001, Provisional));

		assert_ok!(Broker::force_transfer(RuntimeOrigin::root(), region_id, NEW_OWNER));

		let region = Regions::<Test>::get(region_id).unwrap();
		System::assert_last_event(
			Event::Transferred {
				region_id,
				duration: region.end - region_id.begin,
				old_owner: Some(OLD_OWNER),
				owner: Some(NEW_OWNER),
			}
			.into(),
		);
	});
}
```
