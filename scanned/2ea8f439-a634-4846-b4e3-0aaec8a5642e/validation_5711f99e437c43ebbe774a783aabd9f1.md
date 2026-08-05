### Title
Unguarded division-by-zero in validator/nominator reward-share calculation causes duplicated (100%) payouts - ([File: substrate/frame/staking/src/pallet/impls.rs])

### Summary
`do_payout_stakers_by_page` computes each staker's share of an era's reward using `Perbill::from_rational(x, exposure.total())` without ever checking that `exposure.total()` is non-zero. `PerThing::from_rational` does not panic or return zero on a zero denominator — it silently saturates to `Self::one()` (100%). If a validator's paged exposure snapshot ever has `total() == 0` while it still has non-zero era reward points, every nominator's `nominator_exposure_part` (and the validator's own `validator_exposure_part`/`page_stake_part`) collapses to `100%`, so the same `validator_leftover_payout`/`validator_total_commission_payout` amount is paid out in full to the validator AND to each nominator in the page — a duplicated-settlement bug rather than a proportional split.

### Finding Description
The vulnerable calculation: [1](#0-0) 

```
let validator_exposure_part = Perbill::from_rational(exposure.own(), exposure.total());
let validator_staking_payout = validator_exposure_part * validator_leftover_payout;
let page_stake_part = Perbill::from_rational(exposure.page_total(), exposure.total());
let validator_commission_payout = page_stake_part * validator_total_commission_payout;
...
for nominator in exposure.others().iter() {
    let nominator_exposure_part = Perbill::from_rational(nominator.value, exposure.total());
    let nominator_reward: BalanceOf<T> = nominator_exposure_part * validator_leftover_payout;
    ...
}
```

The only earlier guard is `validator_reward_points.is_zero()` — it does **not** guard `exposure.total()`: [2](#0-1) 

`Perbill::from_rational` (and every `PerThing::from_rational`) is documented and tested to saturate to `one()` when the denominator is zero, rather than erroring or returning zero: [3](#0-2) [4](#0-3) 

```
assert_eq!($name::from_rational(1 as $num_type, 0), $name::one());
```

So `Perbill::from_rational(anything, 0) == Perbill::one()` (100%), for both the numerator `0` and any nonzero numerator. Consequently, if `exposure.total()` is ever `0` for a page that is nonetheless being paid out (validator had reward points but its recorded/paged exposure snapshot sums to zero — e.g. through pruning/paging edge cases, a slashed-to-zero own stake combined with an empty/zeroed `others` page, or any storage state where `own + Σ others.value == 0`), then:
- `validator_exposure_part` = 100% ⇒ validator gets the *full* `validator_leftover_payout` instead of its `own/total` share.
- `page_stake_part` = 100% ⇒ validator gets the *full* commission for that page.
- For **every** nominator in `exposure.others()`, `nominator_exposure_part` = 100% ⇒ each nominator independently receives the *full* `validator_leftover_payout`, not a fraction of it.

This is a straight "duplicate settlement / wrong beneficiary amount" defect: the reward pot conservation invariant (`Σ shares == 100%`) is violated because the zero-denominator case is treated as "1" for each claimant instead of being rejected or treated as "0 share for everyone." Note that the newer `staking-async` pallet explicitly recognizes this exact class of zero-denominator hazard and defends against it by checking `sum_weighted_points.is_zero()` / `total_weight.is_zero()` before computing a `Perbill::from_rational` share, emitting an `Unexpected` event and skipping payout instead of computing a share: [5](#0-4) 

The legacy `pallet-staking::do_payout_stakers_by_page` has no equivalent guard on `exposure.total()`.

### Impact Explanation
If `exposure.total() == 0` is reachable for a page that is still eligible for payout (validator_reward_points nonzero, page valid, not yet claimed), each of `payout_stakers`/`payout_stakers_by_page` calls made by any nominator or validator for that page would each independently mint/transfer the *entire* `validator_leftover_payout` (and the validator's own commission share at 100%) rather than a fractional share. This is over-minting/duplicate payout of staking rewards straight from `T::RewardRemainder`/currency issuance, directly matching the "duplicate settlement or payout" / "theft or unbacked mint" impact class for chain-level runtime bugs.

### Likelihood Explanation
This requires no privileged actor: `payout_stakers`/`payout_stakers_by_page` are public, unsigned-origin-free dispatchables callable by anyone for any validator/era/page once an exposure and reward points exist. The only prerequisite is a runtime/storage state where a validator's paged `own`+`others` sum is zero while its era reward points are non-zero — this is a narrower precondition than the `KangarooVault` totalSupply==0 case, and I could not fully confirm within this session whether normal era-transition logic (bonding minimums, exposure snapshot ordering, or paging/pruning) can currently produce such a state in `pallet-staking`. This should be treated as **medium-likelihood / needs further confirmation**: the root defect (unguarded `Perbill::from_rational(_, exposure.total())`) is proven and exploitable *if* the zero-total precondition is reachable, but reachability of that precondition through normal validator lifecycle (chilling, slashing to zero, exposure pruning across pages) was not fully verified against the current index.

### Recommendation
Before computing `Perbill::from_rational(_, exposure.total())` in `do_payout_stakers_by_page`, explicitly check `exposure.total().is_zero()` and short-circuit (return `Ok`/skip payout, or return an error) exactly as the `staking-async` pallet already does for its analogous `sum_weighted_points`/`total_weight` zero-denominator checks, instead of letting `Perbill::from_rational` silently saturate to `one()`.

### Proof of Concept
Conceptual PoC (would need to be validated/executed by a Devin session with repo access):
1. Construct or force a validator's `EraInfo::get_paged_exposure(era, &stash, page)` to return an exposure whose `own() == 0` and `others()` is empty or sums to `0`, while `ErasRewardPoints::<T>::get(&era).individual` still records non-zero points for that validator (e.g. via an era-boundary/slashing/pruning sequence, or directly via test storage manipulation as in `substrate/frame/staking/src/tests.rs`).
2. Call `Staking::payout_stakers_by_page(origin, validator_stash, era, page)`.
3. Observe that `Perbill::from_rational(0, 0)` (or `Perbill::from_rational(x, 0)`) evaluates to `Perbill::one()`, so `validator_staking_payout == validator_leftover_payout` and (if `others()` is nonempty with any nonzero `value` but `total()` still computed as `0` due to a stale/inconsistent metadata field) each nominator's reward also equals the full `validator_leftover_payout`, exceeding the era's allocated reward budget and paying out more than once from the same pot — mirroring the `getTokenPrice()`/`totalSupply()==0` division-by-zero analog from the external report, but manifesting as duplicated over-payment instead of a revert/lock.

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L321-329)
```rust
		let era_reward_points = <ErasRewardPoints<T>>::get(&era);
		let total_reward_points = era_reward_points.total;
		let validator_reward_points =
			era_reward_points.individual.get(&stash).copied().unwrap_or_else(Zero::zero);

		// Nothing to do if they have no reward points.
		if validator_reward_points.is_zero() {
			return Ok(Some(T::WeightInfo::payout_stakers_alive_staked(0)).into());
		}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L343-379)
```rust
		let validator_leftover_payout =
			validator_total_payout.defensive_saturating_sub(validator_total_commission_payout);
		// Now let's calculate how this is split to the validator.
		let validator_exposure_part = Perbill::from_rational(exposure.own(), exposure.total());
		let validator_staking_payout = validator_exposure_part * validator_leftover_payout;
		let page_stake_part = Perbill::from_rational(exposure.page_total(), exposure.total());
		// validator commission is paid out in fraction across pages proportional to the page stake.
		let validator_commission_payout = page_stake_part * validator_total_commission_payout;

		Self::deposit_event(Event::<T>::PayoutStarted {
			era_index: era,
			validator_stash: stash.clone(),
			page,
			next: EraInfo::<T>::get_next_claimable_page(era, &stash, &ledger),
		});

		let mut total_imbalance = PositiveImbalanceOf::<T>::zero();
		// We can now make total validator payout:
		if let Some((imbalance, dest)) =
			Self::make_payout(&stash, validator_staking_payout + validator_commission_payout)
		{
			Self::deposit_event(Event::<T>::Rewarded { stash, dest, amount: imbalance.peek() });
			total_imbalance.subsume(imbalance);
		}

		// Track the number of payout ops to nominators. Note:
		// `WeightInfo::payout_stakers_alive_staked` always assumes at least a validator is paid
		// out, so we do not need to count their payout op.
		let mut nominator_payout_count: u32 = 0;

		// Let's now calculate how this is split to the nominators.
		// Reward only the clipped exposures. Note this is not necessarily sorted.
		for nominator in exposure.others().iter() {
			let nominator_exposure_part = Perbill::from_rational(nominator.value, exposure.total());

			let nominator_reward: BalanceOf<T> =
				nominator_exposure_part * validator_leftover_payout;
```

**File:** substrate/primitives/arithmetic/src/per_things.rs (L384-391)
```rust
	#[must_use]
	fn from_rational<N>(p: N, q: N) -> Self
	where
		N: RationalArg + TryInto<Self::Inner> + TryInto<Self::Upper>,
		Self::Inner: Into<N>,
	{
		Self::from_rational_with_rounding(p, q, Rounding::Down).unwrap_or_else(|_| Self::one())
	}
```

**File:** substrate/primitives/arithmetic/src/per_things.rs (L1309-1319)
```rust
			macro_rules! per_thing_from_rationale_approx_test {
				($num_type:tt) => {
					// within accuracy boundary
					assert_eq!(
						$name::from_rational(1 as $num_type, 0),
						$name::one(),
					);
					assert_eq!(
						$name::from_rational(1 as $num_type, 1),
						$name::one(),
					);
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
