### Title
Re-pooling a still-owned Provisional Region in `pallet-broker` double-counts `InstaPoolIo`, permanently diluting/locking InstaPool revenue payouts - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

### Summary
The Smilee bug's root cause is that share-holder accounting (`totalDeposit`) was tracked per address and mutated additively/subtractively without first netting out prior state tied to the same underlying position when that position could be re-acted upon (via transfer) before settlement. `pallet-broker`'s Instantaneous Coretime Pool (`InstaPool`) accounting has the same structural flaw: `do_pool` mutates the global `InstaPoolIo` ledger additively every time it is called on a `RegionId`, but never reverses the previous `InstaPoolIo` contribution when a `Provisional` region that is already pooled is pooled again (by the same or a new owner after a `transfer`). This inflates the recorded pool size used as the payout denominator, without a corresponding increase in actual purchased coretime.

### Finding Description
`Regions::<T>` entries are the NFT-like ownership record for a purchased Coretime Region (`substrate/frame/broker/src/types.rs:66-74`, `Transferred` event/`do_transfer` at `substrate/frame/broker/src/dispatchable_impls.rs:230-253`). Regions can be freely transferred by their owner via the public `transfer` extrinsic, analogous to an ERC20 `transfer`.

When a region is contributed to the InstaPool with `Finality::Provisional`, `utilize()` keeps the region alive in `Regions` storage after servicing the call: [1](#0-0) 

`do_pool` then unconditionally increments `InstaPoolIo` at the region's `begin`/`end` timeslices and **overwrites** (not merges/guards) the `InstaPoolContribution` for that `region_id`: [2](#0-1) 

Nothing in `do_pool` checks whether `InstaPoolContribution::<T>::get(&region_id)` already exists before accruing `InstaPoolIo` again. The only place that reverses a prior contribution's `InstaPoolIo` effect is `force_unpool_region`, which is called from `do_partition`, `do_interlace`, and `do_assign` — but **not** from `do_pool` itself: [3](#0-2) 

Because a `Provisional` pool leaves the Region NFT intact and transferable, an attacker can:
1. Purchase/own a Region, call `pool(region_id, payee = self, Provisional)`. This accrues `InstaPoolIo(begin).private += size` and `InstaPoolIo(end).private -= size`, and inserts `InstaPoolContribution`.
2. Without unpooling (no `assign`/`partition`/`interlace`/final `pool` call happens), call `pool(region_id, payee = self, Provisional)` again (or `transfer` the region to a second colluding/self-controlled account first, then have that account call `pool` again). `utilize()` succeeds because the region is still present in `Regions` (Provisional reinsert), the owner check passes, and `do_pool` accrues `InstaPoolIo` a second time for the exact same physical Region, while `InstaPoolContribution` for that `region_id` is simply overwritten.

`InstaPoolIo` deltas are exactly what get folded into `InstaPoolHistory`'s `private_contributions`, which the test suite confirms directly (each `PoolIoRecord.private` accrual moves `private_pool_size`/`private_contributions` shown in `insta_pool_history_works`): [4](#0-3) 

`private_contributions` is later used as the payout denominator in `do_claim_revenue`: [5](#0-4) 

Because the same physical core capacity has been double-counted in the denominator (`private_contributions`) without any matching doubling of real coretime supplied, every legitimate contributor's `p = total_payout * contributed_parts / private_contributions` payout is diluted. Existing tests (`instapool_payouts_cannot_be_duplicated_through_partition/interlacing/reassignment`) prove the team defended against duplication via `partition`/`interlace`/`assign` redispatch paths by calling `force_unpool_region` first — but the same defensive call is absent from `do_pool` itself, leaving the direct "pool again while still pooled" path unguarded, exactly like the original ERC20 Vault bug where `transfer`+withdraw bypassed the invariant that other code paths (deposit) enforced correctly.

### Impact Explanation
This corrupts the shared `InstaPoolIo`/`InstaPoolHistory.private_contributions` state, which is chain-global accounting used to settle real DOT revenue payouts to every InstaPool contributor for a timeslice, not just the attacker. The result is under-payment to honest contributors and residual, undistributed `maybe_payout` balances that are eventually force-burned via `do_drop_history` once `contribution_timeout` elapses — i.e., permanent loss of contributor funds/broken payout settlement, matching the "duplicate settlement/payout" and "permanent user-fund lock" impact classes in scope.

### Likelihood Explanation
`pool`/`transfer` are ordinary public, unprivileged extrinsics available to any Region owner; no validator, collator, relayer, or governance actor is required. The only precondition is holding/owning a purchased Region and re-invoking `pool` with `Finality::Provisional` before finalizing it (or transferring the still-pooled region to another self-controlled account and repeating). This is a low-cost, deterministic, repeatable action for any Coretime Region owner.

### Recommendation
In `do_pool` (`substrate/frame/broker/src/dispatchable_impls.rs`), before accruing `InstaPoolIo`/inserting `InstaPoolContribution`, call `force_unpool_region` (or an equivalent guard) to reverse any existing contribution for `region_id`, mirroring the defense already applied in `do_partition`, `do_interlace`, and `do_assign`. Alternatively, reject `do_pool` outright if `InstaPoolContribution::<T>::contains_key(&region_id)` is already true, forcing callers to unpool explicitly first.

### Proof of Concept
Given the existing test harness in `substrate/frame/broker/src/tests.rs` (pattern from `instapool_payouts_cannot_be_duplicated_through_partition`):
```rust
#[test]
fn instapool_io_can_be_duplicated_by_repooling_provisional_region() {
    TestExt::new().endow(1, 1000).execute_with(|| {
        assert_ok!(Broker::do_start_sales(100, 1));
        advance_to(2);
        let region_id = Broker::do_purchase(1, u64::max_value()).unwrap();

        // First (Provisional) pool call.
        assert_ok!(Broker::do_pool(region_id, None, 2, Finality::Provisional));
        let region = Regions::<Test>::get(&region_id).unwrap(); // still exists (Provisional)
        assert_eq!(InstaPoolIo::<Test>::get(region_id.begin).private, 80);
        assert_eq!(InstaPoolIo::<Test>::get(region.end).private, -80);

        // Region still owned & present -> pool it again without unpooling.
        assert_ok!(Broker::do_pool(region_id, None, 2, Finality::Provisional));

        // BUG: InstaPoolIo has been double-accrued for a single physical Region.
        assert_eq!(InstaPoolIo::<Test>::get(region_id.begin).private, 160); // expected 80
        assert_eq!(InstaPoolIo::<Test>::get(region.end).private, -160);     // expected -80
    });
}
```
This demonstrates that `InstaPoolIo` (and downstream `InstaPoolHistory.private_contributions`) is inflated to twice the real coretime supplied by a single Region, without any additional coretime purchase — the exact accounting-duplication class described in the source report, translated to the pallet-broker InstaPool revenue-settlement path.

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L419-470)
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
				.unwrap_or_default();

			payout.saturating_accrue(p);
			pool_record.private_contributions.saturating_reduce(contributed_parts);

			let remaining_payout = total_payout.saturating_sub(p);
			if !remaining_payout.is_zero() && pool_record.private_contributions > 0 {
				pool_record.maybe_payout = Some(remaining_payout);
				InstaPoolHistory::<T>::insert(r, &pool_record);
			} else {
				InstaPoolHistory::<T>::remove(r);
			}
			if !p.is_zero() {
				Self::deposit_event(Event::RevenueClaimItem { when: r, amount: p });
			}
		}

		if contribution.length > 0 {
			InstaPoolContribution::<T>::insert(region, &contribution);
		}
		T::Currency::transfer(&Self::account_id(), &contribution.payee, payout, Expendable)
			.defensive_ok();
		let next = if last < region.begin + contribution.length { Some(region) } else { None };
		Self::deposit_event(Event::RevenueClaimPaid {
			who: contribution.payee,
			amount: payout,
			next,
		});
		Ok(())
	}
```

**File:** substrate/frame/broker/src/tests.rs (L855-893)
```rust
#[test]
fn insta_pool_history_works() {
	TestExt::new().endow(1, 1000).execute_with(|| {
		// We'll be calling get() on this a lot.
		type Io = InstaPoolIo<Test>;
		assert_ok!(Broker::do_start_sales(100, 1));
		advance_to(2);

		// Buy core to add to pool.
		let region_id = Broker::do_purchase(1, u64::max_value()).unwrap();

		// Ensure InstaPoolIo is zeroed.
		let region = Regions::<Test>::get(&region_id).unwrap();
		assert_eq!(Io::get(region_id.begin), PoolIoRecord { private: 0, system: 0 });
		assert_eq!(Io::get(region.end), PoolIoRecord { private: 0, system: 0 });

		assert_eq!(region_id.begin, 4);

		// Add region to pool with Provisional finality.
		assert_ok!(Broker::do_pool(region_id, None, 2, Provisional));
		// Pool IO registers this region entering and exiting at the correct points.
		assert_eq!(Io::get(region_id.begin), PoolIoRecord { private: 80, system: 0 });
		assert_eq!(Io::get(region.end), PoolIoRecord { private: -80, system: 0 });

		// Ensure the history is correct for a full region. Starts at Timeslice 1 with no capacity
		// (Some(0)) for a region (3 timeslices). Timeslice 4 is the region that we put into the
		// pool, this gives us 80 blocks of on-demand per timeslice for a region (three timeslices).
		// Then we go back to Some(0) when it is removed.
		let timeslice_period: u64 = <Test as Config>::TimeslicePeriod::get();
		let expected_private_history = vec![0, 0, 0, 80, 80, 80, 0];

		// Advance and collate the history starting from the current timeslice.
		let actual_private_history: Vec<_> = (1..8)
			.map(|timeslice| {
				advance_to(timeslice as u64 * timeslice_period);
				InstaPoolHistory::<Test>::get(timeslice).unwrap().private_contributions
			})
			.collect();
		assert_eq!(actual_private_history, expected_private_history);
```
