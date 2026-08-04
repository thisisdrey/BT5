### Title
Premature advancement of Instantaneous Pool revenue-claim cursor causes permanent loss of a contributor's pro-rata payout - (File: substrate/frame/broker/src/dispatchable_impls.rs)

### Summary
`pallet-broker`'s `do_claim_revenue` iterates over the timeslices covered by a contributor's `InstaPoolContribution` and, for each timeslice, unconditionally advances the claim cursor (`region.begin`) and decrements the remaining claimable length (`contribution.length`) *before* checking whether that timeslice's revenue has actually been finalized (`pool_record.maybe_payout`). When the payout for a timeslice is not yet known, the function `break`s out of the loop, but the cursor/length mutations for that timeslice have already been committed and are persisted back to storage. The result is that the contributor's claim to that specific timeslice's share of the pool is silently and permanently dropped, even though the pro-rata revenue for it may later become available.

This mirrors the Wildcat root cause at a structural level: a shared/batched pool's accounting state (here, the claim cursor and remaining length that gate a contributor's pro-rata share) is advanced without waiting for the state that legitimizes the settlement (there, `normalizedAmountPaid`; here, `maybe_payout`) to actually be final for that unit, so the contributor's rightful share is not settled exactly once and correctly - it is dropped instead of deferred.

### Finding Description
`Pallet::do_claim_revenue` in `substrate/frame/broker/src/dispatchable_impls.rs`: [1](#0-0) 

The loop body does the following, in this exact order, for every timeslice `r` in `region.begin..last`:
1. `region.begin = r + 1;` — advance the cursor past `r`.
2. `contribution.length.saturating_dec();` — reduce the remaining claimable length.
3. Look up `InstaPoolHistory::<T>::get(r)`. If absent, `continue` (assumes already settled/removed).
4. If present but `pool_record.maybe_payout` is `None` (i.e., `process_revenue` in `tick_impls.rs` has not yet resolved a payout for timeslice `r`, see `substrate/frame/broker/src/tick_impls.rs:97-151`), the function `break`s out of the loop entirely.

Because steps 1 and 2 execute unconditionally *before* the `maybe_payout` check in step 4, by the time the function breaks, the timeslice `r` has already been marked as consumed in the caller's bookkeeping. After the loop, the remaining `contribution` (now starting at `r+1`, with `length` already reduced for `r`) is written back: [2](#0-1) 

Since the contribution key going forward begins at `r+1`, timeslice `r`'s share is never claimable again, even after `process_revenue` eventually finalizes `InstaPoolHistory::<T>::get(r)` with a real `maybe_payout` (as seen in `tick_impls.rs`'s `process_revenue`): [3](#0-2) 

Unlike the withdrawal-batch bug in Wildcat where the flaw let a claimant take *more* than their allotted share by racing state updates, this local analog lets the pool-management bookkeeping race ahead of settlement, causing the contributor's rightful share for a not-yet-settled batch/timeslice to be dropped rather than correctly deferred. In both cases, the shared pool's accounting (batch totals vs. individual claims / cursor vs. per-timeslice availability) is mutated inconsistently with the actual settlement state of the underlying funds.

### Impact Explanation
Any account that has contributed a Region to the Instantaneous Coretime Pool (`do_pool`) and calls `claim_revenue` for a Region whose latest timeslices have not yet had their on-demand revenue processed by `process_revenue` will have those timeslices' entitlements permanently and silently forfeited, without any error being raised (the call succeeds with a partial payout). This is an unbacked reduction of a legitimate contributor's claim — funds remain in the pallet's account and are never paid to the rightful beneficiary for that timeslice, effectively a partial fund lock/loss for an honest, unprivileged actor performing a normal operation (calling `claim_revenue` with `max_timeslices` large enough to reach an unsettled timeslice, or simply calling it slightly too early relative to relay-chain revenue relay).

### Likelihood Explanation
This requires no privileged actor, relayer, validator, or malicious peer — any ordinary contributor who calls `claim_revenue` (a public, permissionless, signed extrinsic) can trigger it simply by claiming before `process_revenue` has resolved payout for the most recent timeslice(s) in their contributed range (e.g., calling immediately after their region ends, or supplying a `max_timeslices` that reaches into recently-elapsed but not-yet-settled timeslices). Given that revenue processing is asynchronous and lags real time (per the pallet's own on-demand revenue relay design), this is a very plausible sequencing to hit in normal usage, not an edge case requiring adversarial coordination.

### Recommendation
Restructure `do_claim_revenue` so `region.begin` and `contribution.length` are only advanced for timeslice `r` *after* confirming a payout has been computed and processed (i.e., move the `maybe_payout` check before mutating the cursor/length), or alternatively `continue`/return without mutating the cursor when `maybe_payout` is `None`, re-inserting the original (unmutated) contribution record so the claim for `r` remains valid for a future call once the payout is settled.

### Proof of Concept
1. `do_reserve`/`do_start_sales`, then `do_purchase` a Region and `do_pool` it (Final) to account `A`, contributing it to the Instantaneous Pool for timeslices `[t, t+len)`.
2. Advance the chain to timeslice `t` (via `advance_to`), which calls `process_pool` and creates `InstaPoolHistory::<T>::get(t)` with `maybe_payout: None` (see `tick_impls.rs::process_pool`).
3. Before any `RevenueInbox` entry is processed for timeslice `t` (i.e., before `process_revenue` sets `maybe_payout = Some(..)` for `t`), call `Broker::claim_revenue(region, max_timeslices)` for `A`'s contribution with `max_timeslices >= 1` covering `t`.
4. Observe: `do_claim_revenue` sets `region.begin = t+1` and decrements `contribution.length` for `t` before hitting the `break` on `pool_record.maybe_payout == None`; the remaining `InstaPoolContribution` is reinserted starting at `t+1`.
5. Later, once relay-chain revenue for timeslice `t` is delivered and `process_revenue` sets `InstaPoolHistory::<T>::get(t).maybe_payout = Some(revenue)`, account `A` can never claim it — `A`'s `InstaPoolContribution` no longer covers `t`, and no other mechanism refunds this to `A`. Contrast with `instapool_partial_core_payouts_work` / `insta_pool_history_works` tests in `substrate/frame/broker/src/tests.rs`, which only cover the "payout already known" path and do not exercise the early-claim-before-settlement race.

### Citations

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L419-456)
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
```

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L458-460)
```rust
		if contribution.length > 0 {
			InstaPoolContribution::<T>::insert(region, &contribution);
		}
```

**File:** substrate/frame/broker/src/tick_impls.rs (L97-151)
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
	}
```
