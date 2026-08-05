### Title
Unchecked `u32` addition in `pallet-staking::reward_by_ids` can overflow era reward points, corrupting validator payout shares - (File: `substrate/frame/staking/src/pallet/impls.rs`)

### Summary
`pallet-staking`'s `reward_by_ids` accumulates per-era validator reward points using plain `+=` on `u32` values with no overflow protection, unlike the functionally identical accumulator in the newer sibling pallet, `pallet-staking-async`, which was patched specifically to replace this pattern with saturating addition.

### Finding Description
`Pallet::<T>::reward_by_ids` mutates `ErasRewardPoints<T>` using raw arithmetic: [1](#0-0) 

Both `era_rewards.individual` (per-validator `u32` points) and `era_rewards.total` (`u32`) are incremented with `+=`, with no `checked_add`/`saturating_add`. This function is fed continuously during block execution by `polkadot/runtime/parachains/src/reward_points.rs`, which credits `BACKING_POINTS` (20) per backed candidate and `DISPUTE_STATEMENT_POINTS` (20) per dispute vote to the elected validator set, via `RewardsReporter::reward_by_ids`: [2](#0-1) 

The exact same class of bug — plain addition on era reward-points accumulators — was identified and fixed in the sibling `pallet-staking-async`, confirming this is a recognized, real bug class in this codebase, not merely a theoretical concern: [3](#0-2) 

The fixed version in `pallet-staking-async::session_rotation::reward_active_era` consistently uses `saturating_accrue`/`saturating_add` for the equivalent `individual` and `total` fields: [4](#0-3) 

`pallet-staking` (the legacy pallet, still active on production relay chains that have not migrated to the Asset-Hub staking flow) never received the equivalent fix — `reward_by_ids` in `impls.rs` still performs unchecked `+=` on both fields.

### Impact Explanation
Polkadot-SDK runtimes are commonly built with `overflow-checks` enabled for the WASM runtime specifically so that integer overflow triggers a deterministic panic rather than a silent wrap. If `ErasRewardPoints::total` or an individual validator's entry in `.individual` reaches `u32::MAX` and is incremented again inside `reward_by_ids`, block execution panics inside the parachains-inclusion/disputes pipeline, which runs on every relay-chain block. A panic during mandatory block-execution logic aborts the block, which can stall or bring down block production on the affected chain — squarely within the "implementation bug that can bring down... a Substrate-based chain" impact category. Even absent a hard panic (if overflow-checks were disabled), a silent wraparound would corrupt `ErasRewardPoints`, causing `EraInfo::<T>::get_validator_commission`/payout-share computations (`Perbill::from_rational(validator_reward_points, total_reward_points)`) to misallocate the era reward budget — a wrong-beneficiary/wrong-amount payout bug in the staking reward-distribution path. [5](#0-4) 

### Likelihood Explanation
Reaching `u32::MAX` (~4.29 billion) accumulated points for a single validator within one active era requires an extremely large number of backing/dispute-vote credits (over 214 million events at 20 points each), which is far beyond what normal block cadence and parachain throughput allow within a single era. This makes the overflow effectively unreachable through ordinary operation today. However, the code path itself contains no defensive guard, and the exact same accumulator pattern was judged worth patching for `pallet-staking-async`, indicating the maintainers consider unchecked accumulation of era points a real defect class rather than a false positive. The residual risk grows if per-block reward multipliers, parachain counts, or era length change in the future.

### Recommendation
Apply the same fix used in `pallet-staking-async` (`PR 9186`) to `pallet-staking::reward_by_ids`: replace `*era_rewards.individual.entry(validator).or_default() += points;` and `era_rewards.total += points;` with `saturating_add` (or `saturating_accrue`) equivalents, matching the pattern already used throughout `RewardPool`/`UnbondPool` in `substrate/frame/nomination-pools/src/lib.rs`.

### Proof of Concept
1. Directly manipulate the `ErasRewardPoints<T>` storage item for an active era for validator `V`, or repeatedly invoke `reward_by_ids` for `V` (e.g., via repeated `reward_backing`/`reward_dispute_statement` calls under test harness) until `individual[V]` (or `total`) equals `u32::MAX`.
2. Trigger one more call: `Pallet::<T>::reward_by_ids(vec![(V, 1)])`.
3. With overflow checks enabled (typical runtime build profile), the `+=` panics, aborting block execution; with overflow checks disabled, the field silently wraps to a small value, corrupting subsequent `payout_stakers_by_page` reward-share calculations for era-based validator/nominator payouts. [6](#0-5)  shows the project's own precedent test pattern (`reward_validator_slashing_validator_does_not_overflow`) for exercising overflow conditions in this exact payout pipeline, confirming the maintainers are aware overflow scenarios in this reward-accounting subsystem are a recognized risk category worth testing.

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L322-337)
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

**File:** polkadot/runtime/parachains/src/reward_points.rs (L43-66)
```rust
	/// Reward validators in session with points, but only if they are in the active set.
	fn reward_only_active(
		session_index: SessionIndex,
		indices: impl IntoIterator<Item = ValidatorIndex>,
		points: u32,
	) {
		let validators = session_info::AccountKeys::<C>::get(&session_index);
		let validators = match validators
			.defensive_proof("account_keys are present for dispute_period sessions")
		{
			Some(validators) => validators,
			None => return,
		};
		// limit rewards to the active validator set
		let active_set: BTreeSet<_> = C::ValidatorSet::validators().into_iter().collect();

		let rewards = indices
			.into_iter()
			.filter_map(|i| validators.get(i.0 as usize).cloned())
			.filter(|v| active_set.contains(v))
			.map(|v| (v, points));

		R::reward_by_ids(rewards);
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

**File:** substrate/frame/staking/src/tests.rs (L2377-2400)
```rust
#[test]
fn reward_validator_slashing_validator_does_not_overflow() {
	ExtBuilder::default().build_and_execute(|| {
		let stake = u64::MAX as Balance * 2;
		let reward_slash = u64::MAX as Balance * 2;

		// Assert multiplication overflows in balance arithmetic.
		assert!(stake.checked_mul(reward_slash).is_none());

		// Set staker
		let _ = asset::set_stakeable_balance::<Test>(&11, stake);

		let exposure = Exposure::<AccountId, Balance> { total: stake, own: stake, others: vec![] };
		let reward = EraRewardPoints::<AccountId> {
			total: 1,
			individual: vec![(11, 1)].into_iter().collect(),
		};

		// Check reward
		ErasRewardPoints::<Test>::insert(0, reward);
		EraInfo::<Test>::set_exposure(0, &11, exposure);
		ErasValidatorReward::<Test>::insert(0, stake);
		assert_ok!(Staking::payout_stakers_by_page(RuntimeOrigin::signed(1337), 11, 0, 0));
		assert_eq!(asset::stakeable_balance::<Test>(&11), stake * 2);
```
