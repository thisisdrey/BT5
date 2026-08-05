## Finding [1](#0-0) 

### Title
Missing zero-check on `total_nominator_stake` before `Perbill::from_rational` allows nominator reward mis-distribution in `payout_from_provider` / `payout_legacy_mint` - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
The reported Solidity bug is a division against a checkpoint `supply` that can legitimately be `0`, causing either a revert or (if guarded incorrectly) a distorted reward share. The direct Substrate analog is in `pallet-staking-async`'s per-page nominator payout helpers, `payout_from_provider` and `payout_legacy_mint`, where `total_nominator_stake` (the denominator of the nominator's reward share) is computed via `saturating_sub` and can collapse to `0` without any guard before being passed straight into `Perbill::from_rational`.

### Finding Description
In `do_payout_stakers_by_page`, the validator's own stake is intentionally read from `ErasStakersOverview` rather than from the page's `exposure.own()`, because "the page's own field is zeroed on pages > 0": [2](#0-1) 

That `overview_own` is then fed into `payout_from_provider` / `payout_legacy_mint`, where the nominator-side denominator is derived as:
```rust
let total_nominator_stake = exposure.total().saturating_sub(overview_own);
``` [3](#0-2) [4](#0-3) 

`saturating_sub` means if `overview_own` is at or above `exposure.total()` (an achievable mismatch since `overview_own` is sourced from a different storage item, `ErasStakersOverview`, than `exposure.total()`, which comes from `Eras::get_paged_exposure`), `total_nominator_stake` silently becomes `0` instead of erroring. Unlike the Solidity report, Substrate's `Perbill::from_rational` doesn't revert on a zero denominator — it clamps/saturates internally, so `Perbill::from_rational(nominator.value, 0)` does not panic but instead produces a saturated (near-100%) share for **every** nominator in the loop:
```rust
for nominator in exposure.others().iter() {
    let nominator_exposure_part = Perbill::from_rational(nominator.value, total_nominator_stake);
    let nominator_reward: BalanceOf<T> = nominator_exposure_part.mul_floor(total_nominator_payout);
    ...
}
``` [5](#0-4) 

Notably, the codebase elsewhere *does* explicitly guard against exactly this class of zero-denominator inconsistency in the sibling function `calculate_validator_incentive_for_page`, which checks `sum_weighted_points.is_zero()` and `total_weight.is_zero()` before calling `Perbill::from_rational` and treats a live budget with zero denominator as a "storage inconsistency" to be surfaced via an event rather than silently computed: [6](#0-5) 

`payout_from_provider` and `payout_legacy_mint` lack this same defensive check, breaking the established pattern in this exact file.

### Impact Explanation
If `total_nominator_stake` reaches `0` while `exposure.others()` is non-empty, every nominator in that page receives a reward computed from a saturated (near-`Perbill::one()`) share of `total_nominator_payout`, instead of their proportional slice. Because the loop iterates over all nominators on the page independently, this results in the *sum* of nominator payouts vastly exceeding `total_nominator_payout` for that page — either transferred out of the era reward pot (`payout_from_provider`, fund drain from a shared pot) or freshly minted (`payout_legacy_mint`, unbacked issuance). This directly breaks the invariant that staking payouts must conserve value and settle exactly once to the correct amount.

### Likelihood Explanation
Likelihood is low, matching the original report's rating: it requires `overview_own` (from `ErasStakersOverview`) to reach or exceed `exposure.total()` (from the paged exposure), which should not happen under correct era-snapshot construction. However, because these two values are sourced from two different storage items populated at (potentially) different points in the era-transition/paging logic, any staking-async election/snapshotting edge case that desynchronizes them silently triggers the saturating-zero path with no defensive check, unlike the sibling incentive-calculation code in the same file.

### Recommendation
Add an explicit zero-check on `total_nominator_stake` before entering the nominator loop in both `payout_from_provider` and `payout_legacy_mint`, mirroring the pattern already used in `calculate_validator_incentive_for_page`: if `total_nominator_stake.is_zero()` while `exposure.others()` is non-empty, treat it as a storage inconsistency (emit `Unexpected`/defensive event and skip payout, or fall back to `exposure.total()` less-defensively-derived own) rather than allowing `Perbill::from_rational` to silently saturate.

### Proof of Concept
Conceptually: construct (or force via test hooks) an era where `ErasStakersOverview::<T>::get(era, &stash).own` is set to a value greater than or equal to `EraInfo`/`Eras::get_paged_exposure(era, &stash, page).total()`, while the page's `others()` list is non-empty. Calling `payout_stakers`/`payout_stakers_by_page` for that (era, stash, page) will compute `total_nominator_stake = 0` via `saturating_sub`, then loop through `exposure.others()` calling `Perbill::from_rational(nominator.value, 0)` for each nominator, producing rewards summing to a large multiple of `total_nominator_payout`. Full exploitation would need to identify or trigger the exact desync condition between `ErasStakersOverview` and the paged exposure snapshot in `staking-async`'s election/snapshot pipeline, which was not verified end-to-end within this investigation — the code-level absence of the zero-guard (in contrast to the guarded sibling function in the same file) is the concrete, directly-cited defect.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L415-425)
```rust
		// Use the overview's own-stake (not the page's, which is zeroed on pages > 0)
		// so the calculator sees the full validator self-stake for reward computation.
		let overview_own =
			ErasStakersOverview::<T>::get(era, &stash).map(|o| o.own).unwrap_or_default();

		let reward_split = T::StakerRewardCalculator::calculate_staker_reward(
			validator_total_payout,
			validator_commission,
			overview_own,
			exposure.total(),
		);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L496-513)
```rust
		let total_nominator_stake = exposure.total().saturating_sub(overview_own);
		for nominator in exposure.others().iter() {
			let nominator_exposure_part =
				Perbill::from_rational(nominator.value, total_nominator_stake);
			let nominator_reward: BalanceOf<T> =
				nominator_exposure_part.mul_floor(total_nominator_payout);

			if let Some((amount, dest)) =
				Self::make_payout_from_provider(era, &nominator.who, nominator_reward)
			{
				nominator_payout_count.saturating_inc();
				Self::deposit_event(Event::<T>::Rewarded {
					stash: nominator.who.clone(),
					dest,
					amount,
				});
			}
		}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L539-542)
```rust
		let total_nominator_stake = exposure.total().saturating_sub(overview_own);
		for nominator in exposure.others().iter() {
			let nominator_exposure_part =
				Perbill::from_rational(nominator.value, total_nominator_stake);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L710-743)
```rust
		let share_part = if Eras::<T>::uses_weighted_points(era) {
			// This validator has non-zero weight (checked above) and reached this point only
			// with non-zero reward points (gated by the caller), so it must have contributed
			// to the denominator. A zero denominator with a live budget is therefore a storage
			// inconsistency and is surfaced rather than silently paying nothing.
			let sum_weighted_points = ErasSumWeightedPoints::<T>::get(era);
			if sum_weighted_points.is_zero() {
				log!(warn, "Sum of weighted points is zero but budget exists for era {}", era);
				Self::deposit_event(Event::<T>::Unexpected(
					UnexpectedKind::ValidatorIncentiveWeightMismatch { era },
				));
				return None;
			}
			let validator_points: RewardPoint =
				era_reward_points.individual.get(stash).copied().unwrap_or(0);
			let numerator = validator_weight.saturating_mul(BalanceOf::<T>::from(validator_points));
			Perbill::from_rational(numerator, sum_weighted_points)
		} else {
			// Legacy stake-only share, denominated by the total incentive weight across all
			// elected validators. A zero denominator with a non-zero budget is a storage
			// inconsistency, so it is surfaced rather than silently paying nothing.
			let total_weight = ErasSumValidatorIncentiveWeight::<T>::get(era);
			if total_weight.is_zero() {
				log!(
					warn,
					"Total validator incentive weight is zero but budget exists for era {}",
					era
				);
				Self::deposit_event(Event::<T>::Unexpected(
					UnexpectedKind::ValidatorIncentiveWeightMismatch { era },
				));
				return None;
			}
			Perbill::from_rational(validator_weight, total_weight)
```
