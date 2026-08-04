Based on my investigation, I found a solid local analog in `pallet-broker`'s Instantaneous Coretime Pool mechanism.

### Title
Region NFT transfer does not carry the right to claim pooled InstaPool revenue, permanently orphaning payout rights from the current NFT owner - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

### Summary
Regions in `pallet-broker` are coretime NFTs that can be freely transferred between accounts (via the pallet's non-fungible implementation). When a Region is placed into the Instantaneous Coretime Pool via `pool`, the caller nominates a `payee` account that is permanently baked into `ContributionRecord` at that moment. Just like the Receivable NFT in the external report, ownership of the underlying claim-bearing asset (the Region) can move to a new holder, but the right to withdraw the associated value (pool revenue) is bound to the stale, originally-recorded address rather than to whoever currently holds the Region.

### Finding Description
`do_pool` records the contribution with a fixed `payee` supplied at call time, independent of the Region's actual ownership going forward: [1](#0-0) 

`do_claim_revenue` later pays out strictly to `contribution.payee`, the address recorded when the region entered the pool — it never re-derives or checks the Region's current owner: [2](#0-1) 

The `claim_revenue` extrinsic itself only requires a signed origin — it performs no ownership check on the caller or the payee, it simply drains the recorded `ContributionRecord` and transfers to whatever `payee` was captured at pool time: [3](#0-2) 

This is structurally identical to the reported bug class: an NFT-like asset (`RegionId`) is transferable, but the entitlement to withdraw accrued value (pool revenue) is anchored to a party recorded at an earlier point in time (the account that pooled it) rather than tracking the asset's current holder. If a Region is sold/transferred after being pooled (which the pallet permits — pooling only assigns the Region to `CoreAssignment::Pool` in the workplan; it does not lock transfers of the `RegionId` record in `Regions`), the new owner of the Region has no mechanism to redirect the `payee`, and the seller (or whoever was named as payee) continues to receive/claim the pooled revenue indefinitely, while the new Region owner who paid for it gets nothing from the pool contribution tied to that period.

### Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary" invariant for the Coretime pool revenue-sharing mechanism. Revenue tied to a coretime Region can be permanently and repeatedly (until length is exhausted) directed to a stale, non-owning account after the Region asset changes hands, causing legitimate purchasers of pooled Regions to lose the reward stream that is economically bundled with the Region, and misrepresenting who is entitled to draw funds from the Broker pallet's pot.

### Likelihood Explanation
Any unprivileged, signed account can trigger this by: (1) pooling a Region with itself as `payee`, (2) transferring/selling the Region NFT to a third party, and (3) continuing to call `claim_revenue` (or simply waiting since payout goes automatically to the recorded `payee`) to collect the pool revenue that the new owner reasonably expects to receive. No admin, governance, relayer, or malicious-node assumption is required — it is purely a public dispatch/state design gap.

### Recommendation
Either (a) prevent pooling from decoupling revenue rights from the Region's current ownership by re-validating/refreshing `payee` against the live Region owner at claim time, or (b) disallow/void transfer of a Region while it has an outstanding `InstaPoolContribution`, mirroring how `do_pool`/`do_assign` already force-unpool provisionally pooled regions before reassignment (see `pr_4081.prdoc`). At minimum, `do_claim_revenue` should not blindly trust a `payee` set once at pool time if the Region can independently change hands afterward.

### Proof of Concept
1. Account `A` purchases a Region and calls `pool(region, None, payee = A, Final)`, creating `ContributionRecord { length, payee: A }` in `InstaPoolContribution`.
2. `A` sells/transfers the underlying Region NFT to account `B` (Regions are transferable NFTs managed by the broker pallet's nonfungible implementation; nothing in `do_pool` locks the Region against transfer).
3. Revenue accrues in `InstaPoolHistory` for the timeslices the Region was pooled.
4. `A` (or anyone, since `claim_revenue` has no ownership gate beyond `ensure_signed`) calls `claim_revenue(region, max_timeslices)`.
5. `do_claim_revenue` pays out to `contribution.payee == A`, even though `B` now owns the Region and reasonably expects the associated pool revenue — reproducing the exact "current NFT holder cannot withdraw associated funds because they are bound to a stale original party" pattern from the external report.

### Citations

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

**File:** substrate/frame/broker/src/lib.rs (L838-848)
```rust
		#[pallet::call_index(12)]
		#[pallet::weight(T::WeightInfo::claim_revenue(*max_timeslices))]
		pub fn claim_revenue(
			origin: OriginFor<T>,
			region_id: RegionId,
			max_timeslices: Timeslice,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			Self::do_claim_revenue(region_id, max_timeslices)?;
			Ok(Pays::No.into())
		}
```
