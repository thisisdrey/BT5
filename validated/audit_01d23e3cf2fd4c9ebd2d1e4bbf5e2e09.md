This confirms the claim is accurate: `utilize` re-inserts the `Regions` record when `finality == Finality::Provisional` [1](#0-0) , and `do_pool` writes `InstaPoolContribution` for a Region pooled via `Broker::pool` (or `assign` with `CoreAssignment::Pool` semantics) with `Finality::Provisional` [2](#0-1) , meaning the Region record continues to exist and be owned/transferable while a live `InstaPoolContribution` entry and `InstaPoolIo` schedule deltas reference it. `do_transfer` neither reads nor removes this contribution before flipping `region.owner`, unlike `do_partition`/`do_interlace`/`do_assign`, which all call `Self::force_unpool_region` first [3](#0-2) [4](#0-3) [5](#0-4) [6](#0-5) .

Audit Report

## Title
`pallet-broker::do_transfer` moves Region ownership without unpooling an active InstaPool contribution, decoupling pooled revenue entitlement from asset ownership - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

## Summary
`do_transfer`, which backs both the `transfer` and `force_transfer` extrinsics, changes `region.owner` and re-inserts the `RegionRecord` without ever calling `force_unpool_region`, unlike `do_partition`, `do_interlace`, and `do_assign`, all of which call it immediately after the owner check. Because `InstaPoolContribution` and `InstaPoolIo` are keyed by `region_id` and set at pooling time (`do_pool`), a Region that was provisionally pooled and then transferred retains a live contribution/schedule tied to the previous owner while legal ownership passes to a new account with no on-chain reconciliation.

## Finding Description
`utilize` re-inserts the `Regions` entry when `finality == Finality::Provisional`, so a Region pooled via `Broker::pool(..., Finality::Provisional)` remains present in `Regions` with its original owner, while `do_pool` has already written `InstaPoolContribution::<T>::insert(&region_id, ContributionRecord { length, payee })` and adjusted `InstaPoolIo` deltas at `region_id.begin` and `region.end`. `do_transfer` only performs `Regions::<T>::get`, an owner check (skipped for `force_transfer`), a mutation of `region.owner`, and a re-insert — it never touches `InstaPoolContribution` or `InstaPoolIo`. In contrast, `do_partition`, `do_interlace`, and `do_assign` all call `Self::force_unpool_region(region_id, &region, &status)` before altering the Region, specifically to reconcile these derived structures for a Region that "was pooled provisionally and it is being redispatched." `transfer`/`force_transfer` is absent from that documented redispatch set, yet transferring ownership is exactly the kind of Region-state change that should invalidate a stale pooling commitment made by the old owner. No guard in `do_transfer` checks `InstaPoolContribution::<T>::contains_key(region_id)`.

## Impact Explanation
This produces an accounting-conservation defect on the Coretime pallet: the `InstaPoolContribution` record's `payee` field and the `InstaPoolIo` schedule become permanently decoupled from actual Region ownership after a transfer. The new owner acquires a Region that, if later redispatched (`assign`/`partition`/`interlace`), triggers `force_unpool_region` to reconcile `InstaPoolIo` against a contribution the new owner never made and has no visibility into, and revenue claimable via `do_claim_revenue` for the pool period remains payable to the original (stale) `payee`, not necessarily reflecting the current owner's expectations. This matches a "duplicate settlement/decoupled entitlement" class defect on pallet-broker state, which is deployed on Coretime-Westend and other coretime runtimes.

## Likelihood Explanation
Both `transfer` (signed, owner-only) and `force_transfer` (AdminOrigin) are reachable via a simple two-step, fully permissionless sequence for `transfer`: pool a Region provisionally with `assign`/`pool` (owner-only, no privilege needed), then call `transfer`. No race condition, relayer, or governance action is required for the `transfer` path.

## Recommendation
Call `Self::force_unpool_region(region_id, &region, &status)` in `do_transfer` before mutating `region.owner`, mirroring `do_partition`/`do_interlace`/`do_assign`, so any live InstaPool contribution is settled/removed and `InstaPoolIo` is reconciled at transfer time.

## Proof of Concept
1. Owner A purchases a Region `region_id` via `do_purchase`.
2. A calls `Broker::pool(region_id, payee_A, Finality::Provisional)` (or the analogous provisional pool assignment), causing `do_pool` to insert `InstaPoolContribution::<T>::insert(region_id, ContributionRecord { payee: A, length })` and adjust `InstaPoolIo` at `region_id.begin`/`region.end`, while `utilize` re-inserts the `Regions` record because `finality == Provisional`.
3. A calls `transfer(region_id, B)` (or AdminOrigin calls `force_transfer`). `do_transfer` succeeds; `Regions::<T>::get(region_id).owner == Some(B)`.
4. Assert `InstaPoolContribution::<T>::get(region_id)` is still `Some(ContributionRecord { payee: A, .. })` post-transfer — the contribution and `InstaPoolIo` deltas remain tied to A even though B now legally owns the Region. Subsequent redispatch by B (`assign`/`partition`/`interlace`) triggers `force_unpool_region` against A's stale contribution.

### Citations

**File:** substrate/frame/broker/src/utility_impls.rs (L107-139)
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
	}
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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L273-276)
```rust
		// Remove this region from the pool in case it has been assigned provisionally. If we get
		// this far then it is still in `Regions` and thus could only have been pooled
		// provisionally.
		Self::force_unpool_region(region_id, &region, &status);
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L303-306)
```rust
		// Remove this region from the pool in case it has been assigned provisionally. If we get
		// this far then it is still in `Regions` and thus could only have been pooled
		// provisionally.
		Self::force_unpool_region(region_id, &region, &status);
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L334-337)
```rust
			// Remove this region from the pool in case it has been assigned provisionally. If we
			// get this far then it is still in `Regions` and thus could only have been pooled
			// provisionally.
			Self::force_unpool_region(region_id, &region, &status);
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
