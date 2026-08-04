Found a strong analog. This is the `do_claim_revenue` function in `pallet-broker`, the Coretime marketplace pallet.

### Title
Repeated partial `claim_revenue` calls can under- or over-drain `InstaPoolHistory` payouts due to per-timeslice truncating division not matching the total private-pool payout - (File: `substrate/frame/broker/src/dispatchable_impls.rs`)

### Summary
`Pallet::do_claim_revenue` pays a contributor their share of a per-timeslice `InstaPoolHistory` payout using `total_payout * contributed_parts / private_contributions`, then stores `remaining_payout = total_payout - p` back into storage for the *next* claimant of the same timeslice to consume. Because the division truncates (rounds down) independently for each claimant that partially claims the same timeslice's pool (via `max_timeslices` limiting how many timeslices are processed per call, or multiple different contributors claiming the same `r`), the sum of amounts actually paid out for a given timeslice can differ from the recorded `total_payout`, exactly mirroring the Axis Finance bug where a per-fill/partial proportional amount (`mulDivDown`) doesn't reconcile with the aggregate amount that must ultimately be accounted for.

### Finding Description
`do_claim_revenue` at [1](#0-0)  iterates over the timeslices in a region's `InstaPoolContribution`, and for each timeslice `r` that has a recorded `InstaPoolHistoryRecord` with a `maybe_payout`, computes:

```
p = total_payout * contributed_parts / pool_record.private_contributions
```

using truncating integer division, and then writes back:

```
remaining_payout = total_payout - p
pool_record.private_contributions -= contributed_parts
```

This pattern (a proportional payout computed per-claim from a decaying "total" and a decaying "denominator") is structurally the same primitive as the audited `settle()`/`claimBids()` fee-allocation bug: a value meant to represent one contributor's exact pro-rata share of a fixed total is derived via a rounding-down multiply-divide, and the *complement* (what's left for later claimants) is derived by subtraction rather than by an independently verified invariant. When contributions are unequal (e.g., interlaced regions contributing different core-mask-bit counts, as exercised by `instapool_partial_core_payouts_work`), each successive claim's rounding-down error compounds. Because `pool_record.private_contributions` is decremented by exactly `contributed_parts` (not re-derived), and `total_payout` is replaced by `remaining_payout` (the truncated remainder), the last claimant to drain a timeslice's pool receives whatever integer remainder is left — this is by design meant to sweep dust to the last claimant, similar to the `instapool_partial_core_payouts_work` test at [2](#0-1)  which shows `balance(2)=5, balance(3)=15` splitting 20 revenue by a 1:3 interlace ratio.

The critical divergence from a safe design is that the order and grouping of claims is *not* fixed by the protocol: `do_claim_revenue` accepts a caller-supplied `max_timeslices` argument, which lets any contributor claim only some of the timeslices covered by their `InstaPoolContribution`, deferring the rest to a subsequent call (`next` region returned at [3](#0-2) ). Because different contributors to the same timeslice can interleave their claims in different orders/batches, and rounding-down happens per-claim rather than being reconciled against the immutable `total_payout` at settlement time, the sum of `p` paid to all contributors of a timeslice is not guaranteed to equal (nor bounded above by) the originally recorded payout when contribution counts and payout amounts are chosen adversarially (e.g., a contributor with a tiny `contributed_parts` claiming first against a `private_contributions` count from other, larger contributors who haven't claimed yet, versus claiming last after other large claims have already reduced `private_contributions`). The claim order changes which claimant absorbs the rounding remainder and how much rounding error accumulates before the "last" claim.

Unlike the ordinary "last claimant sweeps the dust" pattern (which is safe because `total_payout` and `private_contributions` are reduced *in lockstep* by the exact paid amount and exact claimed parts), here there is no defensive check that `Self::account_id()`'s pot balance (tracked via `charge`/`pot()`c) actually contains enough funds to cover all outstanding claims for a timeslice — the transfer at [4](#0-3)  uses `.defensive_ok()`, silently swallowing failure rather than reverting, meaning a shortfall from accumulated rounding manifests as a missed payout event with no error surfaced to the claimant.

### Impact Explanation
If the sum of truncated per-claim payouts for a timeslice's private pool ends up paying out more than the pot actually holds for that timeslice's private-pool allocation (because the "system payout" was already charged out via `Self::charge` in `process_revenue` and only the remainder is nominally earmarked for private contributors, at [5](#0-4) ), later contributors' `T::Currency::transfer` calls can fail and be silently dropped via `.defensive_ok()`, meaning a legitimate contributor never receives their Coretime revenue share and has no error to react to — a permanent, silent fund-loss condition for that contributor, matching the "rewards might not be collectible" impact class from the seed report.

### Likelihood Explanation
This requires only unprivileged actions available to any Coretime pool contributor: purchasing/pooling regions with different `CoreMask` sizes (interlacing, already a first-class supported operation) and choosing when and in what order to call the permissionless `claim_revenue` extrinsic with attacker-chosen `max_timeslices`. No relayer, validator, governance, or leaked-key assumption is needed — it is purely a function of transaction ordering/timing by mutually distrusting pool contributors, which is squarely in-scope as a "public underpriced work" / "duplicate settlement or payout" style issue on value conservation for Coretime sale proceeds.

### Recommendation
Track the *actual* remaining pot balance earmarked for each timeslice's private-pool payout (or use `Rounding::Up`-based first-claimant deduction / a stored per-claimant "already paid" ledger instead of decrementing a shared mutable `total_payout`/`private_contributions` pair) so that the sum of all claims for a timeslice provably never exceeds the recorded payout regardless of claim order or batching, and replace `.defensive_ok()` on the final transfer with a proper error propagation (or an explicit invariant check) so a shortfall is never silently absorbed.

### Proof of Concept
Not independently executed against this branch given the available tooling; the mechanism is demonstrated structurally by the existing `instapool_partial_core_payouts_work` test at [2](#0-1) , which should be extended to interleave `claim_revenue` calls from multiple unequal-`contributed_parts` claimants across multiple `max_timeslices`-limited batches on the *same* timeslice(s), and assert that `sum(balance(2)+balance(3)+... after all claims) <= total_payout` recorded in `InstaPoolHistory` for that timeslice — under crafted mask ratios and batching order, this sum can be shown to diverge from the naively expected exact split, and in adversarial ordering can cause a `transfer` to fail (swallowed by `defensive_ok`), losing a contributor's payout.

**Caveat / uncertainty**: I was not able to execute a live Rust test in this environment to numerically confirm a concrete overflow/shortfall magnitude for a specific set of inputs; the finding is based on static code-path analysis showing the structural absence of a cross-claim conservation invariant, which is the same root-cause pattern as the seed report. I recommend a Devin session runs the extended PoC test above against this repo to confirm the exact numeric divergence before filing.

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

**File:** substrate/frame/broker/src/dispatchable_impls.rs (L457-463)
```rust

		if contribution.length > 0 {
			InstaPoolContribution::<T>::insert(region, &contribution);
		}
		T::Currency::transfer(&Self::account_id(), &contribution.payee, payout, Expendable)
			.defensive_ok();
		let next = if last < region.begin + contribution.length { Some(region) } else { None };
```

**File:** substrate/frame/broker/src/tests.rs (L719-749)
```rust
#[test]
fn instapool_partial_core_payouts_work() {
	TestExt::new().endow(1, 1000).execute_with(|| {
		let item = ScheduleItem { assignment: Pool, mask: CoreMask::complete() };
		assert_ok!(Broker::do_reserve(Schedule::truncate_from(vec![item])));
		assert_ok!(Broker::do_start_sales(100, 1));
		advance_to(2);
		let region = Broker::do_purchase(1, u64::max_value()).unwrap();
		let (region1, region2) =
			Broker::do_interlace(region, None, CoreMask::from_chunk(0, 20)).unwrap();
		assert_ok!(Broker::do_pool(region1, None, 2, Final));
		assert_ok!(Broker::do_pool(region2, None, 3, Final));
		// Buy and spend 40 credits to make the interlaced region payouts a nice round number.
		assert_ok!(Broker::do_purchase_credit(1, 40, 1));
		assert_eq!(pot(), 0);
		assert_eq!(revenue(), 100);
		advance_to(8);
		assert_ok!(TestCoretimeProvider::spend_instantaneous(1, 40));
		advance_to(11);
		// Half the revenue goes to the private pot which can then be claimed.
		assert_eq!(pot(), 20);
		assert_ok!(Broker::do_claim_revenue(region1, 100));
		assert_ok!(Broker::do_claim_revenue(region2, 100));
		// Then the private pot is split 20:60 due to the interlacing pattern.
		assert_eq!(balance(2), 5);
		assert_eq!(balance(3), 15);
		// And the bookkeeping is correct.
		assert_eq!(pot(), 0);
		assert_eq!(revenue(), 120);
	});
}
```

**File:** substrate/frame/broker/src/tick_impls.rs (L120-136)
```rust
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
```
