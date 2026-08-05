Based on my investigation, the strongest local analog to the "assumption that total supply of rebase tokens will not decrease" bug class is a missing zero-denominator guard in the staking-async reward-splitting code, which mirrors the pattern that the same crate's authors *did* explicitly guard against in a sibling function.

### Title
Missing zero-denominator guard before `Perbill::from_rational` in nominator payout split causes over-distribution of the reward pot - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
`Pallet::payout_from_provider` and `Pallet::payout_legacy_mint` compute each nominator's share of `total_nominator_payout` using `Perbill::from_rational(nominator.value, total_nominator_stake)`, where `total_nominator_stake = exposure.total().saturating_sub(overview_own)` [1](#0-0) . This mirrors the report's core defect: internal accounting assumes a derived denominator (here, "remaining exposure after own-stake") stays strictly positive whenever the loop it feeds is non-empty, without an explicit check, exactly like `LibTokenizedVault` assuming rebase-token supply never decreases.

### Finding Description
`total_nominator_stake` is computed once per validator as `exposure.total().saturating_sub(overview_own)` and then reused as the denominator for every nominator in `exposure.others()`: [2](#0-1) 
The same pattern exists in the legacy mint-based payout path: [3](#0-2) 

Neither path checks `total_nominator_stake.is_zero()` before calling `Perbill::from_rational`. This is significant because the very same file's newer incentive-weight code explicitly adds this guard for an analogous zero-denominator hazard: [4](#0-3) 
That guard's comment ("A zero denominator with a live budget is therefore a storage inconsistency and is surfaced rather than silently paying nothing") shows the authors are aware that `Perbill::from_rational` with a zero denominator produces an unsafe result and must be defended against — yet the older nominator-split code lacks the same defense.

`Perbill::from_rational(n, d)` in `sp_arithmetic` degrades to `Self::one()` (100%) when `d == 0`, regardless of `n`. If `total_nominator_stake` is ever `0` while `exposure.others()` is non-empty (i.e. `exposure.total() <= overview_own` but the page still lists individual nominator entries — a state that can arise from a mismatch between the validator's recorded `own` stake in `ErasStakersOverview` and the per-page `others` list stored separately in `ErasStakersPaged`, since these are two independently-updated storage items feeding the same `PagedExposure` facade), every nominator in the loop receives `Perbill::one() * total_nominator_payout`, i.e. the *entire* `total_nominator_payout`, not a proportional slice.

### Impact Explanation
If triggered, the reward pot pays out `total_nominator_payout` to *each* nominator in the exposure page rather than splitting it, multiplying the payout by the number of nominators on that page. Because `payout_from_provider` pays from a fixed reward pot/account rather than minting, this can drain the pot beyond what it was funded for, or (in the legacy `payout_legacy_mint` path) mint far more currency into circulation than the era payout intended — both are "duplicate settlement or payout" / "theft or unbacked mint" outcomes explicitly in scope for this program.

### Likelihood Explanation
The trigger condition (`exposure.total() <= overview_own` while `others()` is non-empty) requires the two independently-stored exposure structures (`ErasStakersOverview.own`/`.total` vs the paged `others` list) to become inconsistent. I was not able to fully enumerate, within the remaining investigation budget, a concrete unprivileged call sequence that forces this desync (e.g. via slashing updating one structure but not the other, or via an edge case where a page's `others` entries retain stale/zero-value records). This is the main open uncertainty in this finding — the missing guard is definitively confirmed by code inspection and by contrast with the sibling defended code path, but full exploit reachability from an unprivileged extrinsic sequence needs further tracing of how `ErasStakersOverview` and `ErasStakersPaged` are populated/updated relative to each other (particularly around `OffenceHandler`/slashing application) before this can be escalated with full confidence.

### Recommendation
Add an explicit `if total_nominator_stake.is_zero() { return 0 }` (or equivalent early-return / defensive log+event, matching the style already used in `calculate_validator_incentive_for_page`) in both `payout_from_provider` and `payout_legacy_mint` before computing `Perbill::from_rational(nominator.value, total_nominator_stake)`, so that a zero denominator cannot silently saturate to a 100% share for every nominator on the page.

### Proof of Concept
Not executed — a runtime harness would need to force `ErasStakersOverview::<T>::get(era, &stash).own` to equal or exceed the page's `exposure.total()` metadata while `exposure.others()` remains non-empty (e.g. by manipulating exposure storage directly in a test, or tracing whether the offence/slash application path can update one structure without the other), then call `do_payout_stakers_by_page` for that era/page and observe each nominator receiving the full `total_nominator_payout` instead of a fraction. This step was not completed due to remaining tool-call budget.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L480-516)
```rust
	/// Payout stakers from an era reward pot (transfer-based, no minting).
	fn payout_from_provider(
		era: EraIndex,
		stash: &T::AccountId,
		validator_payout: BalanceOf<T>,
		exposure: &crate::PagedExposure<T::AccountId, BalanceOf<T>>,
		overview_own: BalanceOf<T>,
		total_nominator_payout: BalanceOf<T>,
	) -> u32 {
		let mut nominator_payout_count: u32 = 0;

		if let Some((amount, dest)) = Self::make_payout_from_provider(era, stash, validator_payout)
		{
			Self::deposit_event(Event::<T>::Rewarded { stash: stash.clone(), dest, amount });
		}

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

		nominator_payout_count
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L518-561)
```rust
	/// Legacy mint-based payout for pre-upgrade eras.
	fn payout_legacy_mint(
		era: EraIndex,
		stash: &T::AccountId,
		validator_payout: BalanceOf<T>,
		exposure: &crate::PagedExposure<T::AccountId, BalanceOf<T>>,
		overview_own: BalanceOf<T>,
		total_nominator_payout: BalanceOf<T>,
	) -> u32 {
		let mut nominator_payout_count: u32 = 0;
		let mut total_imbalance = PositiveImbalanceOf::<T>::zero();

		if let Some((imbalance, dest)) = Self::make_payout_legacy(era, stash, validator_payout) {
			Self::deposit_event(Event::<T>::Rewarded {
				stash: stash.clone(),
				dest,
				amount: imbalance.peek(),
			});
			total_imbalance.subsume(imbalance);
		}

		let total_nominator_stake = exposure.total().saturating_sub(overview_own);
		for nominator in exposure.others().iter() {
			let nominator_exposure_part =
				Perbill::from_rational(nominator.value, total_nominator_stake);
			let nominator_reward: BalanceOf<T> =
				nominator_exposure_part.mul_floor(total_nominator_payout);

			if let Some((imbalance, dest)) =
				Self::make_payout_legacy(era, &nominator.who, nominator_reward)
			{
				nominator_payout_count.saturating_inc();
				Self::deposit_event(Event::<T>::Rewarded {
					stash: nominator.who.clone(),
					dest,
					amount: imbalance.peek(),
				});
				total_imbalance.subsume(imbalance);
			}
		}

		T::Reward::on_unbalanced(total_imbalance);
		nominator_payout_count
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L710-722)
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
```
