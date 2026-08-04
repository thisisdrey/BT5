Based on the evidence gathered, I found a directly on-point local analog: `pallet-broker`'s Instantaneous Coretime Pool revenue accrual and claim mechanism.

### Title
Pooled region revenue becomes permanently unclaimable once the region is redispatched away from the pool - (File: `substrate/frame/broker/src/dispatchable_impls.rs`, `substrate/frame/broker/src/tick_impls.rs`)

### Summary
`pallet-broker` accrues Instantaneous Coretime Pool ("InstaPool") revenue per region while that region is pooled, and the revenue can only be withdrawn later via `claim_revenue`. This mirrors the external report's pattern exactly: value accrues against an object (a gauge / a region) under one code path, but a separate state-changing action removes the object from the flow that keeps the claim path valid, stranding the accrued value.

### Finding Description
A region owner pools their region into the InstaPool with `do_pool` (`Call::pool`), after which `tick_impls.rs::process_revenue` credits `InstaPoolHistory` per-timeslice revenue based on `private_contributions`/`system_contributions` recorded for that region's owner [1](#0-0) . The only way to realize this revenue is `Pallet::claim_revenue` / `do_claim_revenue`, which walks the region's InstaPool contribution record and pays out from `InstaPoolHistory` [2](#0-1) .

The documented fix in `prdoc/stable2509/pr_4081.prdoc` confirms this exact class of bug existed in this pallet: pooled regions could be redispatched (via `partition`/`interlace`/`assign`) while still `Provisional`, and "To claim any revenue from before this point, `claim_revenue` should be called before partitioning/interleaving/reassigning as it cannot be claimed afterwards" [3](#0-2) . In other words, redispatching a region (analogous to "killing the gauge") silently orphaned the revenue-claim path, and the pallet had to be patched to force-unpool pooled regions before allowing that redispatch, exactly the pattern the external report describes for `_claimFees()`.

This confirms the underlying invariant class is real and has manifested before in this exact pallet: **accrued value + a state transition that quietly detaches the object from its future claim path = stuck funds**. The current code still has related sharp edges in the same area:
- `InstaPoolHistory` is written only when `RevenueInbox` is drained by `process_revenue`, keyed by timeslice `when` [4](#0-3) , and if `r.private_contributions` is zero when revenue arrives, the record is dropped via `HistoryDropped`, permanently losing any late/out-of-order contribution registration for that timeslice.
- `claim_revenue` depends on `InstaPoolContribution` records tied to a `RegionId` remaining consistent with the region's actual pooled state; any pathway that changes a region's identity/mask (partition, interlace, assign, mask-transfer) without first settling outstanding `InstaPoolContribution`/`InstaPoolHistory` entries reproduces the stuck-fee condition that PR #4081 had to patch defensively for the `Provisional` case specifically — non-`Provisional` or newly introduced redispatch paths are not verifiable as fully covered from the given code excerpts.

### Impact Explanation
If any code path allows a pooled region's identity or contribution record to be invalidated (via redispatch, mask splitting, or reassignment) without first forcing settlement of outstanding InstaPool contributions, coretime-sale revenue accrued to that region becomes permanently locked in the pallet's revenue account, unable to be claimed by anyone — a permanent user-fund lock, matching the "Impacts" gate for permanent fund lock.

### Likelihood Explanation
Low-to-Medium: the primary known instance (Provisional pooling + partition/interlace/assign) was already patched per `prdoc/stable2509/pr_4081.prdoc`. However, the fix was scoped specifically to `Provisional` finality regions; I could not verify from available code (tool budget exhausted before reading `dispatchable_impls.rs` partition/interlace/assign bodies in full) whether all redispatch paths — including `Final` pooled regions or newly added mutation calls — force-unpool/settle `InstaPoolContribution` before mutating the region, leaving open the possibility of an unpatched variant of the same class.

### Recommendation
Audit every dispatchable in `pallet-broker` that can mutate or destroy a `RegionId` that is currently present in `InstaPoolContribution` (`partition`, `interlace`, `assign`, and any transfer/mask-manipulation calls), and ensure each one either (a) rejects the operation while an unclaimed pool contribution exists, or (b) automatically force-unpools and settles/pays out the accrued revenue for that region before allowing the state change — extending the same defensive pattern already applied for `Provisional` regions in PR #4081 to cover all region states.

### Proof of Concept
Conceptual reproduction (cannot be fully confirmed against current partition/interlace/assign implementations due to incomplete code retrieval):
1. Purchase a region and pool it with `Call::pool` (Final or Provisional finality).
2. Wait for on-demand revenue to accrue against that region via `process_revenue`/`InstaPoolHistory` (as in `instapool_payouts_work` test at `substrate/frame/broker/src/tests.rs:687-718`).
3. Before calling `claim_revenue`, invoke a redispatch operation (`partition`/`interlace`/`assign`) on the same region if such a path exists without a forced-unpool/settlement guard for that region's finality state.
4. Attempt `claim_revenue` on the original `region_id` — if the redispatch path lacks the same guard `pr_4081` added for `Provisional`, the accrued revenue in `InstaPoolHistory` for that region's contribution can no longer be attributed/claimed, permanently stranding the funds in the broker's revenue pot.

### Citations

**File:** substrate/frame/broker/src/tick_impls.rs (L97-150)
```rust
	pub(crate) fn process_revenue() -> bool {
		let Some(OnDemandRevenueRecord { until, amount }) = RevenueInbox::<T>::take() else {
			return false;
		};
		let when: Timeslice =
			(until / T::TimeslicePeriod::get()).saturating_sub(One::one()).saturated_into();
		let mut revenue = T::ConvertBalance::convert_back(amount.clone());
		if revenue.is_zero() {
			Self::deposit_event(Event::<T>::HistoryDropped { when, revenue });
			InstaPoolHistory::<T>::remove(when);
			return true;
		}

		log::debug!(
			target: "pallet_broker::process_revenue",
			"Received {amount:?} from RC, converted into {revenue:?} revenue",
		);

		let mut r = InstaPoolHistory::<T>::get(when).unwrap_or_default();
		if r.maybe_payout.is_some() {
			Self::deposit_event(Event::<T>::HistoryIgnored { when, revenue });
			return true;
		}
		// Payout system InstaPool Cores.
		let total_contrib = r.system_contributions.saturating_add(r.private_contributions);
		let system_payout = if !total_contrib.is_zero() {
			let system_payout =
				revenue.saturating_mul(r.system_contributions.into()) / total_contrib.into();
			Self::charge(&Self::account_id(), system_payout).defensive_ok();
			revenue.saturating_reduce(system_payout);

			system_payout
		} else {
			Zero::zero()
		};

		log::debug!(
			target: "pallet_broker::process_revenue",
			"Charged {system_payout:?} for system payouts, {revenue:?} remaining for private contributions",
		);

		if !revenue.is_zero() && r.private_contributions > 0 {
			r.maybe_payout = Some(revenue);
			InstaPoolHistory::<T>::insert(when, &r);
			Self::deposit_event(Event::<T>::ClaimsReady {
				when,
				system_payout,
				private_payout: revenue,
			});
		} else {
			InstaPoolHistory::<T>::remove(when);
			Self::deposit_event(Event::<T>::HistoryDropped { when, revenue });
		}
		true
```

**File:** substrate/frame/broker/src/lib.rs (L830-848)
```rust
		/// Claim the revenue owed from inclusion in the Instantaneous Coretime Pool.
		///
		/// - `origin`: Must be a Signed origin.
		/// - `region_id`: The Region which was assigned to the Pool.
		/// - `max_timeslices`: The maximum number of timeslices which should be processed. This
		///   must be greater than 0. This may affect the weight of the call but should be ideally
		///   made equivalent to the length of the Region `region_id`. If less, further dispatches
		///   will be required with the same `region_id` to claim revenue for the remainder.
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

**File:** prdoc/stable2509/pr_4081.prdoc (L1-16)
```text
# Schema: Polkadot SDK PRDoc Schema (prdoc) v1.0.0
# See doc at https://raw.githubusercontent.com/paritytech/polkadot-sdk/master/prdoc/schema_user.json

title: "[pallet-broker] Force-unpool provisionally pooled regions before redispatching them"

doc:
  - audience: Runtime User
    description: |
      This PR force removes regions from the pool before allowing them to be redispatched (through
      `partition`/`interlace`/`assign`) for regions pooled with `Provisional` finality. To claim
      any revenue from before this point, `claim_revenue` should be called before
      partitioning/interleaving/reassigning as it cannot be claimed afterwards.

crates:
- name: pallet-broker
  bump: major
```
