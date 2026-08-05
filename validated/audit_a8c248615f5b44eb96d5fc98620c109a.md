Audit Report

## Title
Unguarded division-by-zero in `do_payout_stakers_by_page` reward-share calculation causes duplicated (100%) payouts when a zero-backed validator is elected - ([File: substrate/frame/staking/src/pallet/impls.rs])

## Summary
`do_payout_stakers_by_page` computes every staker's share of an era's reward via `Perbill::from_rational(x, exposure.total())` without ever checking `exposure.total().is_zero()`. `PerThing::from_rational` saturates to `Self::one()` (100%) on a zero denominator rather than erroring, so if a validator's paged exposure snapshot has `total() == 0` while it still earned nonzero era reward points, the validator and every nominator listed against that page each receive the *full* `validator_leftover_payout` instead of a fractional share.

## Finding Description
The vulnerable calculation is in `do_payout_stakers_by_page`: [1](#0-0) 

The only earlier guard checks `validator_reward_points.is_zero()`, not `exposure.total()`: [2](#0-1) 

`Perbill::from_rational` is documented and tested to saturate to `one()` on a zero denominator: [3](#0-2) [4](#0-3) 

**Reachability of `exposure.total() == 0` with nonzero reward points was confirmed via two mechanisms in this repository:**

1. `sp_npos_elections::seq_phragmen` can elect a candidate with zero backed stake when there are more seats (`validator_count`) than candidates with real backing — this is an explicitly acknowledged, tested behavior: "30 is elected with stake 0. The caller is responsible for stripping this." [5](#0-4) 

2. `pallet-staking`'s `store_stakers_info`/`collect_exposures`/`set_exposure` path does **not** strip zero-stake winners before persisting them into `ErasStakersOverview`/`ErasStakersPaged` — unlike `pallet-elections-phragmen`, which explicitly filters `b.is_zero()` winners before finalizing membership: [6](#0-5) [7](#0-6) 
versus the filtering the sibling elections pallet performs: [8](#0-7) 

A validator whose bonded/active stake has been fully unbonded and withdrawn (without calling `chill`) remains in `Validators<T>` and is thus still returned by `get_npos_targets` (which only checks `Validators::<T>::contains_key`), even though `get_npos_voters` would skip them as a self-voter due to zero weight: [9](#0-8) 
If insufficient other candidates are backed to fill `validator_count` seats, `seq_phragmen` can elect this zero-stake target to fill a remaining slot, producing an `Exposure{ total: 0, own: 0, others: [] }`, which `try_trigger_new_era` → `collect_exposures` → `store_stakers_info` writes unmodified into `ErasStakersOverview`: [10](#0-9) 

Once such a validator is part of the active session validator set, they can author blocks and be credited reward points via `reward_by_ids` independently of exposure/stake. On the next `payout_stakers`/`payout_stakers_by_page` call, `EraInfo::<T>::get_paged_exposure` returns this zero-total exposure: [11](#0-10) 
and `Perbill::from_rational(_, 0)` saturates to `one()` for the validator's own share, the page's commission share, and (if `others` were nonempty) every nominator's share, breaking the reward-pot conservation invariant.

By contrast, `pallet-staking-async` already recognizes this exact hazard class and defends against it explicitly before computing an analogous `Perbill::from_rational`: [12](#0-11) 

## Impact Explanation
If `exposure.total() == 0` occurs for a page still eligible for payout, calling `payout_stakers`/`payout_stakers_by_page` pays the *entire* `validator_leftover_payout` (and 100% of the page's commission) to the validator, and, for any nonzero `others` entries recorded against that page, each nominator independently also receives the full `validator_leftover_payout`. This is an over-minting/duplicate-payout defect straight from the reward pot, matching the "duplicate settlement or payout" / unbacked mint impact class for `pallet-staking`.

## Likelihood Explanation
The dispatchables `payout_stakers`/`payout_stakers_by_page` are public and callable by anyone once eligible state exists — no privileged origin is required to trigger the payout itself. The precondition (a validator elected with zero total exposure) requires either: (a) a chain configuration/period where `validator_count` exceeds the number of well-backed candidates (plausible on smaller/testnets or during validator churn), combined with (b) a candidate who remains registered in `Validators<T>` after fully unbonding without chilling. Both conditions are achievable by an ordinary (unprivileged) staker acting as their own validator candidate; no governance or admin action is required. This raises the precondition from merely theoretical to concretely reachable given the npos-elections test explicitly documents zero-stake winners and `store_stakers_info` performs no filtering, unlike the sibling `pallet-elections-phragmen`.

## Recommendation
Before computing `Perbill::from_rational(_, exposure.total())` in `do_payout_stakers_by_page`, explicitly check `exposure.total().is_zero()` and short-circuit the payout (skip/return early), mirroring the guard already present in `pallet-staking-async`'s analogous computation. Additionally, consider filtering zero-backed-stake winners out of `collect_exposures`/`store_stakers_info` in `pallet-staking`, consistent with the filtering already performed in `pallet-elections-phragmen`.

## Proof of Concept
1. Configure a test runtime where `ValidatorCount` exceeds the number of candidates with nonzero backing (mirroring `elect_has_no_entry_barrier` in `substrate/primitives/npos-elections/src/tests.rs`).
2. Register a validator candidate, bond a minimal amount, call `validate()`, then fully `unbond`/`withdraw_unbonded` without calling `chill`, leaving them in `Validators<T>` with zero active stake.
3. Trigger a new era so that `try_trigger_new_era` → `T::ElectionProvider::elect` → `collect_exposures` → `store_stakers_info` elects and persists this candidate with `Exposure{ total: 0, own: 0, others: [] }` in `ErasStakersOverview`/`ErasStakersPaged`.
4. Ensure this validator authors at least one block in the era (or otherwise receives `reward_by_ids` points) so `ErasRewardPoints` records nonzero points for them.
5. Call `Staking::payout_stakers_by_page(origin, validator_stash, era, page)` and assert that `Perbill::from_rational(0, 0)` evaluates to `Perbill::one()`, causing the validator to receive the full `validator_leftover_payout` rather than a proportional (zero, in this case) share — demonstrating the unguarded division-by-zero saturation defect.

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

**File:** substrate/frame/staking/src/pallet/impls.rs (L641-694)
```rust
	pub(crate) fn try_trigger_new_era(
		start_session_index: SessionIndex,
		is_genesis: bool,
	) -> Option<BoundedVec<T::AccountId, MaxWinnersOf<T>>> {
		let election_result = if is_genesis {
			// This pallet only supports single page elections.
			let result = <T::GenesisElectionProvider>::elect(0)
				.map_err(|e| {
					log!(warn, "genesis election provider failed due to {:?}", e);
					Self::deposit_event(Event::StakingElectionFailed);
				})
				.ok()?;

			BoundedSupportsOf::<T::ElectionProvider>::try_from_other_bounds(result).ok()?
		} else {
			// This pallet only supports single page elections.
			<T::ElectionProvider>::elect(0)
				.map_err(|e| {
					log!(warn, "election provider failed due to {:?}", e);
					Self::deposit_event(Event::StakingElectionFailed);
				})
				.ok()?
		};

		let exposures = Self::collect_exposures(election_result);
		if (exposures.len() as u32) < MinimumValidatorCount::<T>::get().max(1) {
			// Session will panic if we ever return an empty validator set, thus max(1) ^^.
			match CurrentEra::<T>::get() {
				Some(current_era) if current_era > 0 => log!(
					warn,
					"chain does not have enough staking candidates to operate for era {:?} ({} \
					elected, minimum is {})",
					CurrentEra::<T>::get().unwrap_or(0),
					exposures.len(),
					MinimumValidatorCount::<T>::get(),
				),
				None => {
					// The initial era is allowed to have no exposures.
					// In this case the SessionManager is expected to choose a sensible validator
					// set.
					// TODO: this should be simplified #8911
					CurrentEra::<T>::put(0);
					ErasStartSessionIndex::<T>::insert(&0, &start_session_index);
				},
				_ => (),
			}

			Self::deposit_event(Event::StakingElectionFailed);
			return None;
		}

		Self::deposit_event(Event::StakersElected);
		Some(Self::trigger_new_era(start_session_index, exposures))
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L696-741)
```rust
	/// Process the output of the election.
	///
	/// Store staking information for the new planned era
	pub fn store_stakers_info(
		exposures: BoundedVec<
			(T::AccountId, Exposure<T::AccountId, BalanceOf<T>>),
			MaxWinnersOf<T>,
		>,
		new_planned_era: EraIndex,
	) -> BoundedVec<T::AccountId, MaxWinnersOf<T>> {
		// Populate elected stash, stakers, exposures, and the snapshot of validator prefs.
		let mut total_stake: BalanceOf<T> = Zero::zero();
		let mut elected_stashes = Vec::with_capacity(exposures.len());

		exposures.into_iter().for_each(|(stash, exposure)| {
			// build elected stash
			elected_stashes.push(stash.clone());
			// accumulate total stake
			total_stake = total_stake.saturating_add(exposure.total);
			// store staker exposure for this era
			EraInfo::<T>::set_exposure(new_planned_era, &stash, exposure);
		});

		let elected_stashes: BoundedVec<_, MaxWinnersOf<T>> = elected_stashes
			.try_into()
			.expect("elected_stashes.len() always equal to exposures.len(); qed");

		EraInfo::<T>::set_total_stake(new_planned_era, total_stake);

		// Collect the pref of all winners.
		for stash in &elected_stashes {
			let pref = Validators::<T>::get(stash);
			<ErasValidatorPrefs<T>>::insert(&new_planned_era, stash, pref);
		}

		if new_planned_era > 0 {
			log!(
				debug,
				"new validator set of size {:?} has been processed for era {:?}",
				elected_stashes.len(),
				new_planned_era,
			);
		}

		elected_stashes
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L948-966)
```rust
			} else if Validators::<T>::contains_key(&voter) {
				// if this voter is a validator:
				let self_vote = (
					voter.clone(),
					voter_weight,
					vec![voter.clone()]
						.try_into()
						.expect("`MaxVotesPerVoter` must be greater than or equal to 1"),
				);

				if voters_size_tracker.try_register_voter(&self_vote, &bounds).is_err() {
					// no more space left for the election snapshot, stop iterating.
					Self::deposit_event(Event::<T>::SnapshotVotersSizeExceeded {
						size: voters_size_tracker.size as u32,
					});
					break;
				}
				all_voters.push(self_vote);
				validators_taken.saturating_inc();
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

**File:** substrate/primitives/npos-elections/src/tests.rs (L593-612)
```rust
#[test]
fn elect_has_no_entry_barrier() {
	let candidates = vec![10, 20, 30];
	let voters = vec![(1, vec![10]), (2, vec![20])];
	let stake_of = create_stake_of(&[(1, 10), (2, 10)]);

	let ElectionResult::<_, Perbill> { winners, assignments: _ } = seq_phragmen(
		3,
		candidates,
		voters
			.iter()
			.map(|(ref v, ref vs)| (*v, stake_of(v), vs.clone()))
			.collect::<Vec<_>>(),
		None,
	)
	.unwrap();

	// 30 is elected with stake 0. The caller is responsible for stripping this.
	assert_eq_uvec!(winners, vec![(10, 10), (20, 10), (30, 0),]);
}
```

**File:** substrate/frame/staking/src/lib.rs (L1174-1210)
```rust
	/// Get exposure for a validator at a given era and page.
	///
	/// This builds a paged exposure from `PagedExposureMetadata` and `ExposurePage` of the
	/// validator. For older non-paged exposure, it returns the clipped exposure directly.
	pub fn get_paged_exposure(
		era: EraIndex,
		validator: &T::AccountId,
		page: Page,
	) -> Option<PagedExposure<T::AccountId, BalanceOf<T>>> {
		let overview = <ErasStakersOverview<T>>::get(&era, validator);

		// return clipped exposure if page zero and paged exposure does not exist
		// exists for backward compatibility and can be removed as part of #13034
		if overview.is_none() && page == 0 {
			return Some(PagedExposure::from_clipped(<ErasStakersClipped<T>>::get(era, validator)));
		}

		// no exposure for this validator
		if overview.is_none() {
			return None;
		}

		let overview = overview.expect("checked above; qed");

		// validator stake is added only in page zero
		let validator_stake = if page == 0 { overview.own } else { Zero::zero() };

		// since overview is present, paged exposure will always be present except when a
		// validator only has its own stake and no nominator stake.
		let exposure_page = <ErasStakersPaged<T>>::get((era, validator, page)).unwrap_or_default();

		// build the exposure
		Some(PagedExposure {
			exposure_metadata: PagedExposureMetadata { own: validator_stake, ..overview },
			exposure_page,
		})
	}
```

**File:** substrate/frame/staking/src/lib.rs (L1306-1327)
```rust
	/// Store exposure for elected validators at start of an era.
	pub fn set_exposure(
		era: EraIndex,
		validator: &T::AccountId,
		exposure: Exposure<T::AccountId, BalanceOf<T>>,
	) {
		let page_size = T::MaxExposurePageSize::get().defensive_max(1);

		let nominator_count = exposure.others.len();
		// expected page count is the number of nominators divided by the page size, rounded up.
		let expected_page_count = nominator_count
			.defensive_saturating_add((page_size as usize).defensive_saturating_sub(1))
			.saturating_div(page_size as usize);

		let (exposure_metadata, exposure_pages) = exposure.into_pages(page_size);
		defensive_assert!(exposure_pages.len() == expected_page_count, "unexpected page count");

		<ErasStakersOverview<T>>::insert(era, &validator, &exposure_metadata);
		exposure_pages.iter().enumerate().for_each(|(page, paged_exposure)| {
			<ErasStakersPaged<T>>::insert((era, &validator, page as Page), &paged_exposure);
		});
	}
```

**File:** substrate/frame/elections-phragmen/src/lib.rs (L1023-1029)
```rust
					// filter out those who end up with no backing stake.
					let mut new_set_with_stake = winners
						.into_iter()
						.filter_map(
							|(m, b)| if b.is_zero() { None } else { Some((m, to_balance(b))) },
						)
						.collect::<Vec<(T::AccountId, BalanceOf<T>)>>();
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
