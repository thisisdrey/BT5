## Analysis

The Sherlock report's core broken invariant is: **an unguarded division by a value that can legitimately be zero silently corrupts a payout share, and nothing detects or blocks the resulting mis-settlement.** In `CvgRewards`, this manifested as a revert/DoS. In `polkadot-sdk`'s arithmetic primitives, the analogous defect manifests differently but is just as real: `PerThing::from_rational` (used for `Perbill`, `Percent`, etc.) does **not** treat a zero denominator as an error condition to the caller — it silently **saturates to `Self::one()` (100%)**.

### Title
Zero-exposure validators cause `Perbill::from_rational` to silently return 100%, duplicating validator payout across every exposure page - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
`PerThing::from_rational(p, q)` is documented and implemented to fall back to `Self::one()` whenever the denominator `q` is zero, rather than surfacing an error to on-chain logic: [1](#0-0) 
The zero-denominator branch inside `from_rational_with_rounding` returns `Err(())`, and the outer `from_rational` unconditionally maps that error to `Self::one()`: [2](#0-1) 
This is explicitly tested and confirmed to also apply when the numerator is non-zero over a zero denominator (`from_rational(1, 0) == one()`): [3](#0-2) 

`pallet-staking-async`'s reward payout path uses this primitive, unguarded, to compute each page's share of a validator's total payout: [4](#0-3) 

### Finding Description
`do_payout_stakers_by_page` computes `page_stake_part = Perbill::from_rational(exposure.page_total(), exposure.total())` and then pays `page_stake_part.mul_floor(reward_split.validator_payout)` to the validator/nominators on **every page separately** (payout is claimed page-by-page via separate calls, gated only by `ClaimedRewards`/`is_rewards_claimed` per page): [5](#0-4) [6](#0-5) 

`exposure.total()` is the sum of the validator's own stake plus all its paged nominators' stake, as recorded in the era snapshot. Page count (`ErasStakersOverview::page_count`) is derived from the *number* of nominators bounded by `MaxExposurePageSize`, independent of the stake amount each nominator contributes. If a validator's own stake and every nominator's exposed `value` are zero (e.g. `MinValidatorBond`/`MinNominatorBond` configured to zero, or nominal-value nominators under a low-ED/zero-ED asset), `exposure.total()` is `0` while `exposure_page_count` can still be `> 1`.

In that state, **every** page's `Perbill::from_rational(page_total=0, total=0)` call falls into the zero-denominator branch and returns `Perbill::one()` (100%), instead of `0` or an error. As a direct consequence, `page_stake_part.mul_floor(reward_split.validator_payout)` pays out the **full** `validator_payout` on every single page, rather than the intended pro-rated fraction per page. The same unguarded pattern exists in the legacy `pallet-staking` payout code: [7](#0-6) 

Unlike the Sherlock case (which reverts/DoS's), here there is no revert at all — the call succeeds and silently mis-prices the reward split, because `Perbill::from_rational` was designed to "never overflow" by clamping to `one()`, without any caller-side guard for the zero-exposure case. The codebase does show elsewhere that the pallet's own authors recognize this exact class of hazard and add explicit zero-denominator guards with an `Unexpected` event before calling `from_rational` — e.g. in `calculate_validator_incentive_for_page`: [8](#0-7) 
but the `page_stake_part` computation at line 429 has no equivalent guard.

### Impact Explanation
If reachable, this produces duplicate settlement: the validator (and the `payout_from_provider`/`payout_legacy_mint` nominator split derived from `validator_staker_payout_for_page`) receives its full era payout amount multiplied by the number of exposure pages instead of once, minting or transferring reward funds beyond what the era's inflation/incentive budget allocated. This directly matches the required-impact category of "theft or unbacked mint" / "duplicate settlement or payout."

### Likelihood Explanation
This requires `exposure.total() == 0` for an elected, multi-page validator, which in turn requires `MinValidatorBond` and effectively-zero nominator stake to be permitted by runtime configuration — an edge/config-dependent condition, not attacker-controlled in the default Polkadot/Kusama parameterization. This mirrors the Sherlock finding's own acknowledged likelihood profile ("unlikely but possible... still Medium because impact is severe and irrecoverable via normal flow"): the guard is entirely absent in code, so if the precondition is ever hit (config change, or a future asset/parachain-specific low-ED staking config), the mis-payout is deterministic and silent, with no defensive check to catch it, unlike the sibling `calculate_validator_incentive_for_page` function that explicitly guards the same class of zero-denominator inconsistency.

### Recommendation
Add an explicit zero-check around `exposure.total()` before computing `validator_exposure_part` / `page_stake_part` (and the nominator-loop equivalents in `payout_from_provider`/`payout_legacy_mint`), mirroring the guard-and-`Unexpected`-event pattern already used in `calculate_validator_incentive_for_page`, e.g.:
```rust
let page_stake_part = if exposure.total().is_zero() {
    Perbill::zero() // or skip payout / emit Unexpected event
} else {
    Perbill::from_rational(exposure.page_total(), exposure.total())
};
```
More generally, consider hardening `PerThing::from_rational` call sites across the staking/rewards code path to treat a zero denominator as a defensive/`Unexpected` condition rather than relying on the library's silent `one()` fallback.

### Proof of Concept
1. Configure (or reach via runtime upgrade/testing harness) a validator `V` with `own = 0` and multiple nominators each with `value = 0` in the era's `ErasStakersOverview`/`ErasStakersPaged` (achievable if `MinValidatorBond == 0` and `MinNominatorBond == 0`), such that `exposure.total() == 0` but `exposure_page_count(era, V) > 1`.
2. Ensure `V` earned non-zero era reward points so `do_payout_stakers_by_page` proceeds past the `validator_reward_points.is_zero()` check. [9](#0-8) 
3. Call `payout_stakers_by_page(era, V, page=0)`. `page_stake_part = Perbill::from_rational(0, 0) = Perbill::one()`, so the full `reward_split.validator_payout` is transferred/minted.
4. Call `payout_stakers_by_page(era, V, page=1)`. `ClaimedRewards` for page 1 is untouched by the page-0 call, so this succeeds and again computes `page_stake_part = Perbill::one()`, transferring the full `reward_split.validator_payout` a second time.
5. Repeat for each remaining page — total amount paid to `V` equals `page_count × validator_payout` instead of `validator_payout`.

### Citations

**File:** substrate/primitives/arithmetic/src/per_things.rs (L385-391)
```rust
	fn from_rational<N>(p: N, q: N) -> Self
	where
		N: RationalArg + TryInto<Self::Inner> + TryInto<Self::Upper>,
		Self::Inner: Into<N>,
	{
		Self::from_rational_with_rounding(p, q, Rounding::Down).unwrap_or_else(|_| Self::one())
	}
```

**File:** substrate/primitives/arithmetic/src/per_things.rs (L699-722)
```rust
			fn from_rational_with_rounding<N>(p: N, q: N, r: Rounding) -> Result<Self, ()>
			where
				N: Clone
					+ Ord
					+ TryInto<Self::Inner>
					+ TryInto<Self::Upper>
					+ ops::Div<N, Output = N>
					+ ops::Rem<N, Output = N>
					+ ops::Add<N, Output = N>
					+ ops::AddAssign<N>
					+ Unsigned
					+ Zero
					+ One
					+ $crate::MultiplyRational,
				Self::Inner: Into<N>
			{
				// q cannot be zero.
				if q.is_zero() { return Err(()) }
				// p should not be bigger than q.
				if p > q { return Err(()) }

				let max: N = $max.into();
				max.multiply_rational(p, q, r).ok_or(())?.try_into().map(|x| $name(x)).map_err(|_| ())
			}
```

**File:** substrate/primitives/arithmetic/src/per_things.rs (L1310-1318)
```rust
				($num_type:tt) => {
					// within accuracy boundary
					assert_eq!(
						$name::from_rational(1 as $num_type, 0),
						$name::one(),
					);
					assert_eq!(
						$name::from_rational(1 as $num_type, 1),
						$name::one(),
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-391)
```rust
		if Eras::<T>::is_rewards_claimed(era, &stash, page) {
			return Err(Error::<T>::AlreadyClaimed
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)));
		}

		Eras::<T>::set_rewards_as_claimed(era, &stash, page);

		let exposure = Eras::<T>::get_paged_exposure(era, &stash, page).ok_or_else(|| {
			Error::<T>::InvalidEraToReward
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		})?;
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L400-403)
```rust
		// Nothing to do if they have no reward points.
		if validator_reward_points.is_zero() {
			return Ok(Some(T::WeightInfo::payout_stakers_alive_staked(0)).into());
		}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L427-431)
```rust
		// Prorate the validator's reward (commission + own-stake share) across pages
		// proportional to each page's stake relative to total.
		let page_stake_part = Perbill::from_rational(exposure.page_total(), exposure.total());
		let validator_staker_payout_for_page =
			page_stake_part.mul_floor(reward_split.validator_payout);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L455-473)
```rust
		let nominator_payout_count: u32 = if use_dap_payout {
			Self::payout_from_provider(
				era,
				&stash,
				validator_staker_payout_for_page,
				&exposure,
				overview_own,
				reward_split.nominator_payout,
			)
		} else {
			Self::payout_legacy_mint(
				era,
				&stash,
				validator_staker_payout_for_page,
				&exposure,
				overview_own,
				reward_split.nominator_payout,
			)
		};
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L710-744)
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
		};
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L343-350)
```rust
		let validator_leftover_payout =
			validator_total_payout.defensive_saturating_sub(validator_total_commission_payout);
		// Now let's calculate how this is split to the validator.
		let validator_exposure_part = Perbill::from_rational(exposure.own(), exposure.total());
		let validator_staking_payout = validator_exposure_part * validator_leftover_payout;
		let page_stake_part = Perbill::from_rational(exposure.page_total(), exposure.total());
		// validator commission is paid out in fraction across pages proportional to the page stake.
		let validator_commission_payout = page_stake_part * validator_total_commission_payout;
```
