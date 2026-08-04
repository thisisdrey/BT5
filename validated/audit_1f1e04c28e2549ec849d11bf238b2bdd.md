### Title
Unchecked `+=` on `EraRewardPoints` in legacy `pallet-staking::reward_by_ids` allows silent `u32` wraparound of reward-point accounting - (File: `substrate/frame/staking/src/pallet/impls.rs`)

### Summary
`Pallet::<T>::reward_by_ids` in the legacy `pallet-staking` accumulates era reward points using plain `+=` instead of a saturating/checked operation, unlike every other reward/payout accumulator in the surrounding staking, staking-async, nomination-pools, and Snowbridge code (which consistently use `saturating_add`/`checked_add`/`defensive_saturating_sub`). This is the direct local analog of the `SafeMath not completely used` finding: an accumulator that should be bounded is left to standard arithmetic, so it will panic in debug builds or silently wrap in release builds when it overflows `u32::MAX`.

### Finding Description
`reward_by_ids` mutates `ErasRewardPoints::<T>` for the active era: [1](#0-0) 

Both `era_rewards.individual` (per-validator `u32` point counter) and `era_rewards.total` are incremented with raw `+=`, with no `saturating_add`/`checked_add`/`defensive_saturating_accrue`. This is inconsistent with the rest of the codebase's reward-accounting logic, which was deliberately hardened against this exact class of bug:

- `pallet-staking-async`'s equivalent function, `Eras::reward_active_era`, uses `saturating_accrue` for both the individual and total counters: [2](#0-1) 
- A dedicated PR doc confirms this was an intentional fix applied to `pallet-staking-async` specifically to close this overflow: [3](#0-2) 
- All downstream payout arithmetic in the legacy `pallet-staking` payout path (`validator_total_payout`, `validator_leftover_payout`, etc.) is careful to use `Perbill`-scaled/`defensive_saturating_sub` operations: [4](#0-3) 

`reward_by_ids` is reachable from `Pallet::note_author` (the `pallet_authorship::EventHandler` implementation), which is invoked automatically on every block for the current block author/uncle-referencers, and is also directly callable as a `pub(crate)`/internal helper wherever block-authorship credit is awarded. Existing guards do not stop wraparound here: there is no bound check before the `+=`, no cap on `points`, and the storage type for `EraRewardPoints.individual`/`.total` is a plain `u32` with no saturating wrapper (`RewardPoint = u32`). Once either counter reaches `u32::MAX`, the next increment wraps to a small value (release mode) or panics the runtime (debug mode built with overflow checks) — either outcome corrupts the reward-point ledger used to compute payout shares in `Eras::<T>::get_reward_points` and the `Perbill::from_rational(validator_reward_points, total_reward_points)` payout split.

### Impact Explanation
Reward points feed directly into `do_payout_stakers_by_page`'s `Perbill::from_rational(validator_reward_points, total_reward_points)` share calculation, which determines how the era's DOT/KSM reward budget is split among validators/nominators. A wraparound of `individual`/`total` corrupts this ratio for the affected era, causing wrong-beneficiary/wrong-amount payouts (some validators over- or under-paid relative to their actual authored blocks), i.e., a real "runtime bug that compromises intended behavior" / mis-accounted payout under the program's impact gate. In a debug/overflow-checked runtime build, the wraparound instead panics the node during block execution of `on_initialize`/session rotation, which is a chain-halting condition.

### Likelihood Explanation
Reaching `u32::MAX` (~4.29 billion) points within a single era through ordinary block authorship is impractical under normal parameters (blocks per era are bounded, and points-per-block are small constants), so exploiting this purely through legitimate block production is currently very low probability. However, this is precisely the class of bug the external report flags: an accumulator that is *not* uniformly guarded by saturating/checked arithmetic while everywhere else in the same subsystem (and in the hardened successor pallet, `pallet-staking-async`) is. It represents inconsistent defensive-programming discipline in a live payout-critical code path, and is a straightforward, low-cost fix (as already demonstrated by the `pallet-staking-async` migration in `pr_9186.prdoc`) that removes a latent overflow/panic vector before it can be triggered by any future change to points-per-block constants, custom `EventHandler`/`reward_by_ids` callers, or long-lived chains with cumulative points across many out-of-era carryovers.

### Recommendation
Replace the raw `+=` operators in `reward_by_ids` with `saturating_accrue` (or `checked_add` with a `defensive` fallback) for both `era_rewards.individual` entries and `era_rewards.total`, mirroring the fix already applied in `pallet-staking-async::Eras::reward_active_era`:

```rust
fn reward_by_ids(validators_points: impl IntoIterator<Item = (T::AccountId, u32)>) {
    if let Some(active_era) = ActiveEra::<T>::get() {
        <ErasRewardPoints<T>>::mutate(active_era.index, |era_rewards| {
            for (validator, points) in validators_points.into_iter() {
                era_rewards.individual.entry(validator).or_default().saturating_accrue(points);
                era_rewards.total.saturating_accrue(points);
            }
        });
    }
}
```

### Proof of Concept
1. In a debug/overflow-checks-enabled test build of `pallet-staking`, seed `ErasRewardPoints::<Test>::individual` (or `.total`) for a validator to `u32::MAX - 1`.
2. Call `Pallet::<Test>::reward_by_ids(vec![(validator, 2)])` twice (or once with `points = 2`).
3. Observe an arithmetic overflow panic (debug) or a silent wrap to a value near `0` (release), demonstrating that `era_rewards.total`/`.individual` no longer reflects the true accumulated reward points.
4. Follow with `payout_stakers_by_page` for that era to show the corrupted `total`/`individual` values produce an incorrect `Perbill::from_rational` payout split relative to actual blocks authored, confirming the downstream payout impact.

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L322-351)
```rust
		let total_reward_points = era_reward_points.total;
		let validator_reward_points =
			era_reward_points.individual.get(&stash).copied().unwrap_or_else(Zero::zero);

		// Nothing to do if they have no reward points.
		if validator_reward_points.is_zero() {
			return Ok(Some(T::WeightInfo::payout_stakers_alive_staked(0)).into());
		}

		// This is the fraction of the total reward that the validator and the
		// nominators will get.
		let validator_total_reward_part =
			Perbill::from_rational(validator_reward_points, total_reward_points);

		// This is how much validator + nominators are entitled to.
		let validator_total_payout = validator_total_reward_part * era_payout;

		let validator_commission = EraInfo::<T>::get_validator_commission(era, &ledger.stash);
		// total commission validator takes across all nominator pages
		let validator_total_commission_payout = validator_commission * validator_total_payout;

		let validator_leftover_payout =
			validator_total_payout.defensive_saturating_sub(validator_total_commission_payout);
		// Now let's calculate how this is split to the validator.
		let validator_exposure_part = Perbill::from_rational(exposure.own(), exposure.total());
		let validator_staking_payout = validator_exposure_part * validator_leftover_payout;
		let page_stake_part = Perbill::from_rational(exposure.page_total(), exposure.total());
		// validator commission is paid out in fraction across pages proportional to the page stake.
		let validator_commission_payout = page_stake_part * validator_total_commission_payout;

```

**File:** substrate/frame/staking/src/pallet/impls.rs (L849-857)
```rust
	fn reward_by_ids(validators_points: impl IntoIterator<Item = (T::AccountId, u32)>) {
		if let Some(active_era) = ActiveEra::<T>::get() {
			<ErasRewardPoints<T>>::mutate(active_era.index, |era_rewards| {
				for (validator, points) in validators_points.into_iter() {
					*era_rewards.individual.entry(validator).or_default() += points;
					era_rewards.total += points;
				}
			});
		}
```

**File:** substrate/frame/staking-async/src/session_rotation.rs (L432-455)
```rust
					let recorded = match era_rewards.individual.get_mut(&validator) {
						Some(individual) => {
							individual.saturating_accrue(points);
							true
						},
						None => {
							// not much we can do -- validators should always be less than
							// `MaxValidatorSet`.
							era_rewards.individual.try_insert(validator, points).defensive().is_ok()
						},
					};

					// Keep the denominator aligned with `individual`, which is the source used
					// by payouts and try-state recomputation. A defensive overflow may leave
					// points unrecorded; those points must not be counted in
					// `ErasSumWeightedPoints`.
					if recorded && !weight.is_zero() {
						sum_weighted_points_delta = sum_weighted_points_delta.saturating_add(
							weight.saturating_mul(IncentiveWeight::<T>::from(points)),
						);
					}

					era_rewards.total.saturating_accrue(points);
				}
```

**File:** prdoc/stable2509/pr_9186.prdoc (L1-9)
```text
title: 'pallet-staking-async: Use saturating addition for era reward points'
doc:
- audience: Runtime Dev
  description: |-
    This PR replaces regular addition with saturating addition when accumulating era reward points in
    pallet-staking-async to prevent potential overflow.
crates:
- name: pallet-staking-async
  bump: patch
```
