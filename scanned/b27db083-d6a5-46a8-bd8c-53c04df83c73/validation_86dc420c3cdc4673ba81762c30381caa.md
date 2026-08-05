### Title
Premature `claim_revenue` call permanently forfeits a contributor's Coretime revenue share and dilutes the payout of remaining contributors - ([File: substrate/frame/broker/src/dispatchable_impls.rs])

### Summary
`pallet-broker`'s permissionless `claim_revenue` extrinsic (`Pallet::claim_revenue` → `do_claim_revenue`) advances a contributor's claim cursor (`region.begin`, `contribution.length`) for a timeslice **before** it verifies that revenue for that timeslice has actually been processed (`InstaPoolHistoryRecord::maybe_payout`). If the revenue has not yet arrived from the relay chain (`process_revenue` in `tick_impls.rs` has not yet set `maybe_payout`), the function `break`s out of the loop, but the cursor advancement for that timeslice is never undone. The contributor can never re-claim that timeslice again, and the pool's `private_contributions` counter for that timeslice is never reduced to reflect their departure, which dilutes and locks a portion of pool revenue.

### Finding Description
`do_claim_revenue` in [1](#0-0)  iterates timeslices `r` from `region.begin` to `last = region.begin + contribution.length.min(max_timeslices)`:

```rust
for r in region.begin..last {
    region.begin = r + 1;
    contribution.length.saturating_dec();

    let Some(mut pool_record) = InstaPoolHistory::<T>::get(r) else { continue };
    let Some(total_payout) = pool_record.maybe_payout else { break };
    ...
    pool_record.private_contributions.saturating_reduce(contributed_parts);
    ...
}
```

Crucially, `region.begin = r + 1` and `contribution.length.saturating_dec()` execute unconditionally at the top of the loop body, **before** the code checks whether `pool_record.maybe_payout` is `Some`. Revenue for a given timeslice `r` is populated later, out-of-band, by `Pallet::process_revenue` (called during tick processing) once an `OnDemandRevenueRecord` arrives from the relay chain via `RevenueInbox` — see [2](#0-1) . There is no guarantee revenue has been reported for the most recent timeslices in a contributor's claim range at the moment `claim_revenue` is called; the pallet's own tests show the reporting has a multi-block/timeslice latency (see `drop_history_works`, which explicitly comments on the 1-timeslice + notice-period + processing-block latency for `InstaPoolHistory`) [3](#0-2) .

When `maybe_payout` is still `None` for timeslice `r` (revenue not yet processed), the loop hits `else { break }`. At that point:
- `region.begin` has already been permanently advanced past `r`.
- `contribution.length` has already been permanently decremented for `r`.
- `pool_record.private_contributions` is **not** reduced (the reduction only happens in the branch that computes `p`, which is skipped by the `break`).

The updated `contribution` (with the advanced `region.begin`/decremented `length`) is written back to storage via `InstaPoolContribution::<T>::insert(region, &contribution)` at the end of the function (only if `contribution.length > 0`), and the emitted `next` region for follow-up claims already starts strictly after `r`:
```rust
if contribution.length > 0 {
    InstaPoolContribution::<T>::insert(region, &contribution);
}
...
let next = if last < region.begin + contribution.length { Some(region) } else { None };
```
Since `region.begin` was already advanced past `r`, no future call to `claim_revenue` by this contributor — no matter when it is made, even long after `process_revenue` eventually sets `InstaPoolHistory::<T>::get(r).maybe_payout` — can ever revisit timeslice `r`. Meanwhile the shared `InstaPoolHistoryRecord` at `r` still counts this contributor's `contributed_parts` in `private_contributions`, because that field was never decremented. When `process_revenue`/the eventual payout math for `r` divides the private-pool payout by `private_contributions` (see `do_claim_revenue`'s division: `total_payout * contributed_parts / pool_record.private_contributions`), this departed contributor's share is computed for `pool_record.private_contributions` as a whole but is claimed by nobody: it is deducted from `total_payout` only when someone with a live claim actually calls `claim_revenue` for `r`, and the departed user's `contributed_parts` continue to reduce every other still-active claimant's share (since the divisor `private_contributions` still includes them), permanently locking that fraction of revenue in the broker's pot account (`Self::account_id()`), with no dispatchable path to recover or reclaim it.

### Impact Explanation
This is a public, unprivileged-caller vulnerability (`claim_revenue` is callable by anyone with an `InstaPoolContribution` entry) that causes:
- Permanent loss/lock of a contributor's rightful Coretime pool revenue share (funds get stuck in the broker's pot with no code path to release them for that timeslice/contributor combination).
- Dilution of other contributors' payouts for the same timeslice, since the payout math still divides by a `private_contributions` count that includes the "ghost" share of the departed contributor.
- No malicious/privileged actor is required — a normal contributor triggering `claim_revenue` slightly too early (a very plausible operational sequence, given latency in relay-chain revenue reporting acknowledged by the pallet's own tests) is enough to trigger this permanently.

This matches the "permanent user-fund or bridge-state lock" and "public underpriced/misaccounted state advance without settlement" categories in the impact gate: state (`region.begin`/`contribution.length`) advances past a timeslice without the corresponding settlement (payout transfer + `private_contributions` reduction) completing atomically.

### Likelihood Explanation
High likelihood in practice: any contributor who calls `claim_revenue` for a region whose tail timeslices have not yet had their on-demand revenue processed (a normal, expected race given the pallet's documented multi-block latency for revenue reporting) will trigger this bug automatically, with no special conditions, front-running, or privileged access required. The `max_timeslices` argument is fully attacker/user controlled, so a caller can deliberately request a range that intentionally spans into unprocessed timeslices to force the forfeiting/dilution effect, or simply do so unintentionally through normal usage.

### Recommendation
Do not advance `region.begin`/decrement `contribution.length` for timeslice `r` until it is confirmed that `pool_record.maybe_payout` is `Some` and the payout for `r` has been computed/applied. Restructure the loop so that when `maybe_payout` is `None`, the loop stops **without** consuming `r` (i.e., leave `region.begin` at `r`, not `r + 1`, and don't decrement `contribution.length` for the un-processed timeslice), so a later `claim_revenue` call can still successfully claim `r` once `process_revenue` has populated its payout.

### Proof of Concept
1. `do_pool` a region for account `A`, creating an `InstaPoolContribution` spanning timeslices `[begin, begin+length)`.
2. Advance the chain so that `InstaPoolHistory` entries exist for `begin..begin+length-1` with `maybe_payout = Some(_)` (already processed) but the last timeslice `begin+length-1` has **not** yet had `process_revenue` run for it (i.e., `InstaPoolHistory::get(begin+length-1)` is either missing or has `maybe_payout = None`).
3. Call `claim_revenue(region, length)` (i.e., `max_timeslices = length`, covering the full range).
4. Observe that the loop processes `begin..begin+length-2` normally, then for `r = begin+length-1` it executes `region.begin = r + 1` and `contribution.length.saturating_dec()` before discovering `maybe_payout` is `None` and breaking.
5. Later, once revenue for `begin+length-1` is processed by `process_revenue` (setting `maybe_payout = Some(...)`), call `claim_revenue` again for account `A`'s (now-advanced) `InstaPoolContribution` — observe it is impossible to ever reference timeslice `begin+length-1` again, since the stored contribution's `region.begin` already equals `begin+length` (or the contribution entry has been fully consumed/removed if `length` hit zero). `A`'s revenue share for that timeslice is permanently unclaimable, and `InstaPoolHistory::get(begin+length-1).private_contributions` still counts `A`'s `contributed_parts`, diluting any other contributor still claiming that timeslice.

### Citations

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

**File:** substrate/frame/broker/src/tests.rs (L128-133)
```rust
			advance_to(6);
			// In the stable state with no pending payouts, we expect to see 3 items in
			// InstaPoolHistory here since there is a latency of 1 timeslice (for generating the
			// revenue report), the forward notice period (equivalent to another timeslice) and a
			// block between the revenue report being requested and the response being processed.
			assert_eq!(InstaPoolHistory::<Test>::iter().count(), 3);
```
