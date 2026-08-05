Audit Report

## Title
Re-pooling a still-owned Provisional Region in `pallet-broker` double-counts `InstaPoolIo`, permanently diluting/locking InstaPool revenue payouts - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

## Summary
`do_pool` unconditionally accrues `InstaPoolIo` at `region_id.begin`/`region.end` and overwrites `InstaPoolContribution` for that `region_id` every time it is invoked, without first reversing any pre-existing contribution for that region. Because `utilize()` re-inserts a `Provisional` region back into `Regions` storage, an owner can call `pool(..., Finality::Provisional)` a second time on the same still-owned region and inflate `InstaPoolIo` (and downstream `InstaPoolHistory.private_contributions`) without adding any real coretime, while `InstaPoolContribution` remains a single overwritten record.

## Finding Description
`utilize()` removes the region from `Regions`, and for `Finality::Provisional` re-inserts the identical `region` record under the same `region_id`, leaving it owned and poolable again [1](#0-0) . `do_pool` then accrues `InstaPoolIo` and inserts (overwrites) `InstaPoolContribution` unconditionally, with no check for a pre-existing contribution and no call to `force_unpool_region` [2](#0-1) . In contrast, `do_partition`, `do_interlace`, and `do_assign` all explicitly call `Self::force_unpool_region(region_id, &region, &status)` before re-registering the region, specifically to guard against exactly this double-accrual case when a region was pooled provisionally and is redispatched [3](#0-2) . `force_unpool_region` is the only code path that reverses a prior `InstaPoolIo` contribution when `InstaPoolContribution::<T>::take(region_id)` finds an existing entry [4](#0-3) , and `do_pool` never calls it.

Exploit flow: an owner calls `pool(region_id, Provisional)` once, accruing `InstaPoolIo(begin).private += size` / `InstaPoolIo(end).private -= size` and inserting an `InstaPoolContribution`. Because finality is `Provisional`, the region remains in `Regions` and is still owned by the same account. The owner calls `pool(region_id, Provisional)` again on the identical `region_id` without any intervening `assign`/`partition`/`interlace`/`Finality::Final` call. `utilize()` succeeds again (owner check passes, region still present), and `do_pool` accrues `InstaPoolIo` a second time for the same physical region while `InstaPoolContribution::<T>::insert` merely overwrites the single stored record (not additive), so only one claimable contribution exists but the `InstaPoolIo` ledger — which feeds `InstaPoolHistory.private_contributions`, the payout denominator used in `do_claim_revenue` — has been inflated by 2x relative to actual purchased/poolable coretime [5](#0-4) .

I confirmed the cited code in `do_pool`, `utilize`, and `force_unpool_region` matches the claim exactly, and that `force_unpool_region` is called in `do_partition`/`do_interlace`/`do_assign` but is absent from `do_pool`. This is a genuine asymmetry in the guarded code paths.

## Impact Explanation
This corrupts `InstaPoolIo`, which is folded into `InstaPoolHistory.private_contributions` — the payout denominator in `do_claim_revenue` used to distribute real DOT revenue to InstaPool contributors for a timeslice [6](#0-5) . Inflating this denominator without a matching increase in real supplied coretime dilutes payouts to all other honest contributors for that timeslice and leaves residual undistributed `maybe_payout` balances, which is a duplicate-settlement/payout-corruption class impact reachable via ordinary public extrinsics.

## Likelihood Explanation
`pool` is a public, unprivileged extrinsic callable by any Region owner. The only precondition is owning a purchased Region and calling `pool` with `Finality::Provisional` twice without an intervening finalizing action (`assign`, `partition`, `interlace`, or a `Finality::Final` pool/assign). This requires no special privileges, collusion, or governance action, and is deterministic and repeatable.

## Recommendation
In `do_pool` (`substrate/frame/broker/src/dispatchable_impls.rs`), call `Self::force_unpool_region(region_id, &region, &status)` before accruing `InstaPoolIo`/inserting `InstaPoolContribution`, mirroring the guard already present in `do_partition`, `do_interlace`, and `do_assign`. Alternatively, reject the call if `InstaPoolContribution::<T>::contains_key(&region_id)` already holds an entry, requiring callers to unpool first.

## Proof of Concept
Using the existing `pallet-broker` test harness pattern (`insta_pool_history_works` / `instapool_payouts_cannot_be_duplicated_through_partition`):
1. `do_start_sales`, advance, `do_purchase` a region to get `region_id`.
2. Call `Broker::do_pool(region_id, None, payee, Finality::Provisional)`. Assert `InstaPoolIo::<Test>::get(region_id.begin).private == 80` and `InstaPoolIo::<Test>::get(region.end).private == -80`.
3. Without any `assign`/`partition`/`interlace`/final call, call `Broker::do_pool(region_id, None, payee, Finality::Provisional)` again on the same `region_id` (still present in `Regions` due to Provisional re-insertion, still owned by the same account).
4. Observe `InstaPoolIo::<Test>::get(region_id.begin).private == 160` and `InstaPoolIo::<Test>::get(region.end).private == -160` — double the correct value for a single physical region — while `InstaPoolContribution::<Test>::get(region_id)` still holds only one (overwritten) record, confirming the payout-denominator inflation with no corresponding claimable coretime increase.

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L303-337)
```rust
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

	pub(crate) fn do_assign(
		region_id: RegionId,
		maybe_check_owner: Option<T::AccountId>,
		target: TaskId,
		finality: Finality,
	) -> Result<(), Error<T>> {
		let config = Configuration::<T>::get().ok_or(Error::<T>::Uninitialized)?;
		let status = Status::<T>::get().ok_or(Error::<T>::Uninitialized)?;

		if let Some((region_id, region)) = Self::utilize(region_id, maybe_check_owner, finality)? {
			let workplan_key = (region_id.begin, region_id.core);
			let mut workplan = Workplan::<T>::get(&workplan_key).unwrap_or_default();

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L419-440)
```rust
	pub(crate) fn do_claim_revenue(
		mut region: RegionId,
		max_timeslices: Timeslice,
	) -> DispatchResult {
		ensure!(max_timeslices > 0, Error::<T>::NoClaimTimeslices);
		let mut contribution =
			InstaPoolContribution::<T>::take(region).ok_or(Error::<T>::UnknownContribution)?;
		let contributed_parts = region.mask.count_ones();

		Self::deposit_event(Event::RevenueClaimBegun { region, max_timeslices });

		let mut payout = BalanceOf::<T>::zero();
		let last = region.begin + contribution.length.min(max_timeslices);
		for r in region.begin..last {
			region.begin = r + 1;
			contribution.length.saturating_dec();

			let Some(mut pool_record) = InstaPoolHistory::<T>::get(r) else { continue };
			let Some(total_payout) = pool_record.maybe_payout else { break };
			let p = total_payout
				.saturating_mul(contributed_parts.into())
				.checked_div(&pool_record.private_contributions.into())
```
