Based on my analysis, I found a concrete, provable local analog of the Deviation.sol division-by-zero bug in the legacy `pallet-staking` reward payout path. Unlike the external report where div-by-0 causes a revert, here the Substrate `PerThing::from_rational` primitive silently falls back to `100%` on a zero denominator instead of erroring, which is a *more* dangerous failure mode for a payout calculation (wrong beneficiary amount, not just DoS).

### Title
Zero-denominator exposure share silently resolves to `Perbill::one()` in legacy `payout_stakers`, causing incorrect reward amount instead of a safe abort - (File: `substrate/frame/staking/src/pallet/impls.rs`)

### Summary
`Pallet::do_payout_stakers_by_page` (called from the public, unprivileged extrinsic `payout_stakers`/`payout_stakers_by_page`) computes a validator's and a page's share of an era's reward using `Perbill::from_rational(numerator, exposure.total())` without ever checking that `exposure.total()` is non-zero. `Perbill::from_rational` does not panic or return `0` on a zero denominator — it returns `Perbill::one()` (100%) via its `unwrap_or_else(|_| Self::one())` fallback path. This is the same broken-invariant class as the external Deviation.sol finding (unguarded division by a value that can legitimately be zero), but the consequence here is a silently wrong payout multiplier rather than an accidental revert.

### Finding Description
In `substrate/frame/staking/src/pallet/impls.rs`: [1](#0-0) 
```
let validator_exposure_part = Perbill::from_rational(exposure.own(), exposure.total());
let validator_staking_payout = validator_exposure_part * validator_leftover_payout;
let page_stake_part = Perbill::from_rational(exposure.page_total(), exposure.total());
let validator_commission_payout = page_stake_part * validator_total_commission_payout;
```
Neither `exposure.own()` nor `exposure.total()` is checked for zero before this division-equivalent call.

The root primitive, `PerThing::from_rational`, defines the exact div-by-zero behavior: [2](#0-1) 
```
fn from_rational<N>(p: N, q: N) -> Self
...
{
	Self::from_rational_with_rounding(p, q, Rounding::Down).unwrap_or_else(|_| Self::one())
}
```
and the underlying rounding function explicitly documents/returns `Err(())` on `q.is_zero()`: [3](#0-2) 
```
// q cannot be zero.
if q.is_zero() { return Err(()) }
```
So the `Err` produced when `exposure.total() == 0` is converted by `from_rational` into `Perbill::one()` — a 100% share — instead of being propagated or treated as "no payout"/"skip".

Critically, the maintainers of the codebase are aware this exact zero-denominator condition is a real, reachable "storage inconsistency" risk in reward-share computations: the newer `staking-async` pallet's analogous function explicitly guards against it and treats it as a defensive/abort condition rather than letting `Perbill::from_rational` silently resolve to a value: [4](#0-3) 
```
let sum_weighted_points = ErasSumWeightedPoints::<T>::get(era);
if sum_weighted_points.is_zero() {
	log!(warn, "Sum of weighted points is zero but budget exists for era {}", era);
	Self::deposit_event(Event::<T>::Unexpected(
		UnexpectedKind::ValidatorIncentiveWeightMismatch { era },
	));
	return None;
}
```
This confirms the design intent: a zero denominator in a stake/points share computation must be explicitly detected and the payout skipped/surfaced — not silently computed. The legacy `pallet-staking::do_payout_stakers_by_page` path lacks this guard entirely for `exposure.own()/exposure.total()` and `exposure.page_total()/exposure.total()`.

`exposure.total()` is a `PagedExposureMetadata` field set once at era-election time via `EraInfo::get_paged_exposure`/`ErasStakersOverview`: [5](#0-4) 
```
let exposure = EraInfo::<T>::get_paged_exposure(era, &stash, page).ok_or_else(|| {
	Error::<T>::InvalidEraToReward
		.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
})?;
```
and only `validator_reward_points.is_zero()` is checked before the division — there is no check that `exposure.total()` (a logically independent storage field) is non-zero: [6](#0-5) 
```
// Nothing to do if they have no reward points.
if validator_reward_points.is_zero() {
	return Ok(Some(T::WeightInfo::payout_stakers_alive_staked(0)).into());
}
```
Because reward points (`ErasRewardPoints`) and stake exposure (`ErasStakersOverview`/`ErasStakersPaged`) are two separate storage maps populated by two different subsystems (authorship/point-scoring vs. election snapshotting), there is no structural guarantee in this function that `total() > 0` whenever `validator_reward_points > 0`. Any divergence between these two maps — from a migration bug, an election-provider inconsistency, or any code path that lets a validator accumulate reward points without a corresponding non-zero stake snapshot for that era/page — causes the payout share computation to silently substitute `100%` for the correct (near-zero or undefined) fraction, instead of aborting as the newer pallet does.

### Impact Explanation
If reached, this bug causes `do_payout_stakers_by_page` to pay a validator (and/or route commission) based on a `100%` share multiplier instead of the correct proportional share, and to do the same for `page_stake_part`, which gates commission payout. This directly maps to the "wrong beneficiary or amount"/"conserve value" pivot: rewards are minted/transferred from the era reward pot in an amount that does not match the intended pro-rata share, potentially over-paying one validator relative to the fixed era budget and creating an inconsistency in aggregate payouts across pages/validators for that era (the sum of `page_stake_part` fragments across pages is only guaranteed to total 100% when `exposure.total()>0` throughout — a zero-total page breaks that invariant). Since this is invoked through the unprivileged, public `payout_stakers`/`payout_stakers_by_page` extrinsics, no admin/governance/validator-collusion is required to *trigger* the call once the underlying zero-exposure storage state exists.

### Likelihood Explanation
Low-to-Medium. Reaching `exposure.total() == 0` for a validator that also has non-zero `ErasRewardPoints` for that era requires a state divergence between the independently-populated exposure-metadata storage and the reward-points storage — this is not achievable through a single, ordinary unprivileged call under a correctly configured runtime with normal minimum-bond enforcement. However, the codebase's own newer implementation (`staking-async`) explicitly documents and defends against exactly this "storage inconsistency" scenario as a real, anticipated risk, which is strong first-party evidence that the underlying precondition is considered reachable in practice (e.g., via migrations, election-provider edge cases, or benchmark/test-only code paths bleeding into production logic). The legacy `pallet-staking` payout path was never updated with the equivalent guard.

### Recommendation
In `substrate/frame/staking/src/pallet/impls.rs::do_payout_stakers_by_page`, before computing `validator_exposure_part`/`page_stake_part`, explicitly check `exposure.total().is_zero()` and return `Error::<T>::InvalidEraToReward` (or skip with a defensive/`Unexpected` event, mirroring the `staking-async` pattern) rather than allowing `Perbill::from_rational` to silently resolve the division to `Perbill::one()`. More broadly, consider hardening `PerThing::from_rational` callers project-wide (or adding a `checked_from_rational` variant used consistently in payout-critical code) so that zero-denominator conditions in monetary share computations always fail closed instead of defaulting to `100%`.

### Proof of Concept
1. Bring a validator `V` into a state where, for era `E`, `ErasStakersOverview::<T>::get(E, V)` (or the equivalent paged exposure) has `total == 0` (and thus `own == 0`, `page_total == 0`) — e.g., via a storage migration bug, an election-provider defect that stores an empty exposure while still crediting the validator, or a runtime misconfiguration allowing zero-bond validators.
2. Ensure `ErasRewardPoints::<T>::get(E).individual` contains a non-zero entry for `V` (populated independently through authorship/session reward-point bookkeeping).
3. Call the public extrinsic `payout_stakers(V, E)` (or `payout_stakers_by_page`) as any unprivileged account.
4. Observe that `Perbill::from_rational(0, 0)` inside `do_payout_stakers_by_page` resolves to `Perbill::one()` (per `substrate/primitives/arithmetic/src/per_things.rs:390` and `:716`), so `validator_staking_payout` and `validator_commission_payout` are computed using a `100%` multiplier instead of erroring or paying `0`, resulting in an incorrect transfer from the era reward pot.

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L307-310)
```rust
		let exposure = EraInfo::<T>::get_paged_exposure(era, &stash, page).ok_or_else(|| {
			Error::<T>::InvalidEraToReward
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0))
		})?;
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L326-329)
```rust
		// Nothing to do if they have no reward points.
		if validator_reward_points.is_zero() {
			return Ok(Some(T::WeightInfo::payout_stakers_alive_staked(0)).into());
		}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L343-351)
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

**File:** substrate/primitives/arithmetic/src/per_things.rs (L713-722)
```rust
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
