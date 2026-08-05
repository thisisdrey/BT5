Based on the evidence gathered, I found a strong local analog in `pallet-staking-async`'s new weighted-points validator incentive formula, which distributes an era's fixed incentive budget using a live, incrementally-growing denominator that is not snapshotted per validator at the moment their share is fixed for a claim.

### Title
Validator incentive payout share is diluted by a live-growing `ErasSumWeightedPoints` denominator instead of a per-claim snapshot - (File: substrate/frame/staking-async/src/session_rotation.rs)

### Summary
The new validator self-stake incentive payout formula computes each validator's share of a fixed per-era budget as `share_i = (w_i · ep_i) / Σ_j(w_j · ep_j))`, where the denominator `ErasSumWeightedPoints` is maintained incrementally as session reports arrive and credit era points via `reward_active_era` [1](#0-0) . This is structurally the same broken invariant as the Exactly report: a reward share is computed by dividing an individual's weighted numerator by a pool-wide denominator that keeps changing (growing) over time as more activity (session reports / era points) is credited, before the reward is actually paid out.

### Finding Description
`ErasSumWeightedPoints::<T>::get(era)` accrues across sequential calls to `reward_active_era` within an era, and this is explicitly documented as "the load-bearing property of the incrementally-maintained storage: calls compose, so the denominator at payout time reflects the entire history of `reward_active_era` invocations within the era" [1](#0-0) . Whether an era uses this new weighted formula versus the legacy stake-only formula is decided by `uses_weighted_points`, based on a single cutoff era boundary `WeightedPointsFormulaStartEra` [2](#0-1) .

The core issue: each validator's numerator `w_i · ep_i` is fixed once their own era-points are final, but the denominator `Σ_j(w_j · ep_j)` continues to be mutated by *other* validators' session reports for the same era, as documented by `ErasSumWeightedPoints`. If payout for a given era's page can be triggered (via `payout_stakers`/`make_all_reward_payment`) before all validators in that era have had their session-report era-points fully credited, the payout computed for an early-processed validator uses a denominator that is smaller than its final value — over-crediting early claimants relative to the eventual full-era denominator, and correspondingly under-crediting validators whose points are processed/claimed later, once further `reward_active_era` calls have grown the sum. This mirrors exactly the Exactly Finance bug: identical "shares" (weighted era points) convert to different amounts of the fixed budget depending on when the payout is computed relative to when the shared denominator was last updated by other actors.

The PR description itself frames the denominator explicitly as being "maintained incrementally ... avoiding a full validator iteration at payout time" [3](#0-2) , which is precisely the kind of "live floating denominator instead of a snapshot" pattern flagged in the external report.

### Impact Explanation
If exploitable within the same era window (i.e., if payout pages can be processed while `ErasSumWeightedPoints` for that era is still being incrementally updated by ongoing session reports), this would cause validators to receive incentive payouts that do not sum correctly to the fixed era budget, or that are unfairly skewed based on payout timing rather than actual weighted performance — a runtime bug that compromises the intended reward-distribution behavior (miscalculated/duplicate-adjacent settlement of a treasury-like incentive budget), falling under "runtime bugs that compromise intended behavior" and "duplicate settlement or payout" impact categories.

### Likelihood Explanation
This is **uncertain** given the available evidence. I was not able to fully confirm, within the tool budget, whether `ErasSumWeightedPoints` for an era is guaranteed to be finalized (frozen) strictly before any payout for that era's page can be processed (e.g., via an era-transition boundary check in `calculate_validator_incentive_for_page`), or whether there is a genuine window where a payout call and a still-in-flight `reward_active_era` update for the same era can interleave. The test `sum_weighted_points_accrues_across_sequential_calls` only demonstrates that the sum accrues correctly across sequential calls within the same era — it does not test the ordering guarantee between "all points credited" and "payout allowed." Because of index size limits, I could not fully read `calculate_validator_incentive_for_page` in `substrate/frame/staking-async/src/pallet/impls.rs` to verify whether it enforces era finality before computing shares.

### Recommendation
Verify (and if necessary enforce) that `ErasSumWeightedPoints[era]` and all `ErasRewardPoints[era]` are fully finalized (i.e., the era has fully ended and no further `reward_active_era` calls for it are possible) before any payout page for that era is computed. If not already guaranteed, snapshot the final denominator once at era-close (similar to how `nomination-pools`' `RewardPool` snapshots `last_recorded_reward_counter` as a monotonic accumulator rather than a live mutable ratio) and use that frozen value for all payout-share calculations for that era, rather than a value that can still be mutated after some validators have already been paid.

### Proof of Concept
A concrete PoC could not be fully constructed within the available investigation budget because it depends on confirming the exact call/era-lifecycle ordering guarantees in `calculate_validator_incentive_for_page` (in `substrate/frame/staking-async/src/pallet/impls.rs`), which I was unable to fully inspect due to index/tool limitations. A Devin session with full repository access would be needed to:
1. Read `calculate_validator_incentive_for_page` in full to check whether it can execute for an era before that era's `ErasSumWeightedPoints` is guaranteed final.
2. Construct a test scenario (extending `substrate/frame/staking-async/src/tests/validator_incentive.rs`) that calls `reward_active_era` for validator A, immediately pays out validator A's incentive share, then calls `reward_active_era` for validator B (increasing `ErasSumWeightedPoints`), and pays out B — checking whether the sum of A's and B's payouts diverges from the intended weighted split of the fixed era budget. [1](#0-0) [2](#0-1) [3](#0-2)

### Citations

**File:** substrate/frame/staking-async/src/tests/validator_incentive.rs (L1126-1147)
```rust
#[test]
fn sum_weighted_points_accrues_across_sequential_calls() {
	// The load-bearing property of the incrementally-maintained storage: calls
	// compose, so the denominator at payout time reflects the entire history of
	// `reward_active_era` invocations within the era.
	ExtBuilder::default().build_and_execute(|| {
		let alice = 11;
		setup_incentive_with_budget(45, 5);
		Session::roll_until_active_era(2);

		let alice_weight = ErasValidatorIncentiveWeight::<Test>::get(2, alice).unwrap();

		// WHEN: two back-to-back credits for the same validator.
		Eras::<Test>::reward_active_era(vec![(alice, 3)]);
		assert_eq!(ErasSumWeightedPoints::<Test>::get(2), alice_weight * 3);

		Eras::<Test>::reward_active_era(vec![(alice, 4)]);

		// THEN: sum accrued — w · (3 + 4) = w · 7.
		assert_eq!(ErasSumWeightedPoints::<Test>::get(2), alice_weight * 7);
	});
}
```

**File:** substrate/frame/staking-async/src/session_rotation.rs (L465-495)
```rust
	pub(crate) fn get_reward_points(era: EraIndex) -> EraRewardPoints<T> {
		ErasRewardPoints::<T>::get(era)
	}

	pub(crate) fn get_reward_points_for_validator(
		era: EraIndex,
		validator: &T::AccountId,
	) -> RewardPoint {
		let points = ErasRewardPoints::<T>::get(era);
		points.individual.get(validator).copied().unwrap_or_default()
	}

	/// Whether era `era` uses the weighted-points incentive-share formula
	/// `share_i = (w_i · ep_i) / Σ_j(w_j · ep_j)`.
	///
	/// Returns `true` for eras at or after [`crate::WeightedPointsFormulaStartEra`], and while
	/// the cutoff is still unset before the migration records it.
	///
	/// Returns `false` for pre-cutoff eras, which fall back to the legacy stake-only share
	/// `share_i = w_i / Σ_j w_j`. Those eras may have reward points credited before their
	/// [`crate::ErasSumWeightedPoints`] denominator was maintained; recomputing it for the full
	/// [`Config::HistoryDepth`] window on upgrade would cost `HistoryDepth × MaxValidatorSet`
	/// reads, so the migration sets the cutoff to `active_era + 1` instead. See
	/// [`crate::migrations::SetWeightedPointsFormulaStartEra`].
	///
	/// Single source of truth for the cutoff decision, shared by the payout path
	/// ([`crate::Pallet::calculate_validator_incentive_for_page`]) and [`Self::do_try_state`].
	pub(crate) fn uses_weighted_points(era: EraIndex) -> bool {
		crate::WeightedPointsFormulaStartEra::<T>::get().map_or(true, |start| era >= start)
	}
}
```

**File:** prdoc/pr_11979.prdoc (L1-23)
```text
title: Scale validator self-stake incentive by era-points performance
doc:
- audience: Runtime Dev
  description: |-
    Validator self-stake incentive payout is now distributed proportionally across
    elected validators using the weighted-points share
    `share_i = (w_i · ep_i) / Σ_j(w_j · ep_j)`, where `w_j` is each validator's
    per-era incentive weight and `ep_j` is their era points.

    The denominator is maintained incrementally in `ErasSumWeightedPoints` as
    session reports credit era points, avoiding a full validator iteration at
    payout time. Validators with zero era points or no incentive weight receive
    nothing, and non-zero weighted performers share the era budget subject to
    normal rounding.

    Pre-upgrade pending eras keep paying out under the legacy stake-only share
    `w_i / Σ_j w_j` so they are not zeroed out by the missing
    `ErasSumWeightedPoints` denominator. The cutoff era is recorded by a
    one-shot migration `SetWeightedPointsFormulaStartEra` into a new
    `WeightedPointsFormulaStartEra` storage item; eras strictly older than the
    cutoff use the legacy formula, eras at or after the cutoff use the new
    weighted-points formula. The pallet storage version is bumped (17 → 18) and
    the migration is version-gated.
```
