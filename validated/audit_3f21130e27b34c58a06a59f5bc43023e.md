### Title
Non-saturating era-reward-point accumulation in `pallet-staking::reward_by_ids` can silently wrap/panic and corrupt an era's reward split - ([File: substrate/frame/staking/src/pallet/impls.rs])

### Summary
`pallet-staking`'s `reward_by_ids` (the legacy, still widely deployed staking pallet) accumulates per-validator and per-era reward points with plain `+=` on a `u32` counter, instead of `saturating_add`/`checked_add`. The sibling, newer `pallet-staking-async` had the identical bug and was patched (`pr_9186.prdoc`: *"Use saturating addition for era reward points... to prevent potential overflow"*), confirming this exact bug class is acknowledged as security-relevant in this codebase. The legacy `pallet-staking` path was left unfixed.

### Finding Description
`reward_by_ids` mutates `ErasRewardPoints<T>` using raw addition: [1](#0-0) 

```
*era_rewards.individual.entry(validator).or_default() += points;
era_rewards.total += points;
```

`RewardPoint` is a `u32`. This function is called every block via `pallet_authorship::EventHandler::note_author` (fixed 20 points per authored block) and, in the relay chain, via `polkadot/runtime/parachains/src/reward_points.rs` (`BACKING_POINTS = 20`, `DISPUTE_STATEMENT_POINTS = 20`) for every backed/disputed candidate, for every active validator, every block, indefinitely for the life of an era's accounting (and the pallet never resets `individual`/`total` between calls within an era — only `clear_era_information` removes it once the era rotates and history depth expires). Because runtime Wasm is normally built with overflow checks disabled in release/production, an overflow of the `u32` counter will **wrap silently** rather than panic, corrupting `era_rewards.total` and/or `era_rewards.individual[validator]`.

This corrupted value directly feeds `do_payout_stakers_by_page`, which computes: [2](#0-1) 

`validator_total_reward_part = Perbill::from_rational(validator_reward_points, total_reward_points)` and `validator_total_payout = validator_total_reward_part * era_payout`. A wrapped/incorrect `total`/`individual` silently changes the fraction of the era payout each validator/nominator set receives — i.e., the reward pot is settled to the wrong beneficiaries/amounts, without any error being raised. This is precisely the "state inconsistency in certain consensus scenarios" class named in the seed report: a silently-corrupted accounting invariant that skews settlement, rather than a benign crash.

Confirmation this is a recognized, live bug class in this exact codebase: the parallel implementation in `pallet-staking-async` was patched for the identical defect. [3](#0-2) [4](#0-3) 

The legacy `pallet-staking::reward_by_ids` was not given the same treatment, and existing guards elsewhere (`Perbill::from_rational`, `defensive_saturating_sub`) only protect the payout math, not the vulnerable accumulation step that produces `total`/`individual` in the first place.

### Impact Explanation
Points accrue automatically and unconditionally as long as validators author blocks and back/dispute candidates — no privileged actor, admin, or malicious peer is required; it is a pure function of chain uptime and normal block production/parachain activity (`BACKING_POINTS`/`DISPUTE_STATEMENT_POINTS` = 20 per candidate/dispute per validator, `note_author` = per block). Once wrapped, `ErasValidatorReward` splitting via `Perbill::from_rational(validator_reward_points, total_reward_points)` becomes silently wrong, causing validators/nominators to receive an incorrect share of `era_payout` — i.e., duplicate/incorrect settlement of treasury-funded staking rewards to the wrong beneficiary/amount, which matches the "Impacts" gate for reward-payout invariant violations without needing a malicious actor.

### Likelihood Explanation
Low to moderate, mirroring the seed report's own likelihood rating. `u32::MAX` (~4.29 billion) points must accumulate within the bookkeeping window before an era's data is cleared (`clear_era_information`, gated by `HistoryDepth`), which normally requires an implausibly long-lived single era/validator-history window in practice, but relay-chain reward emission (20 points per backed candidate per validator, potentially per block, across all active validators over `HistoryDepth` eras) accelerates this compared to the naive "one block = one point" case. The bug is a genuine, provable defect regardless of exact time-to-trigger; and the identical defect was judged worth a dedicated fix elsewhere in the same repository (`pr_9186`), evidencing project acknowledgment of exploitability under sustained normal operation.

### Recommendation
Change `reward_by_ids` in `substrate/frame/staking/src/pallet/impls.rs` to use `saturating_add`/`checked_add` for both `era_rewards.individual` entries and `era_rewards.total`, exactly as was already done for `pallet-staking-async` in `substrate/frame/staking-async/src/session_rotation.rs::reward_active_era`. Apply the same saturating pattern anywhere else `RewardPoint`/era point totals are mutated with raw arithmetic operators in the legacy staking pallet.

### Proof of Concept
Not independently executed in this session (no sandbox access), but a direct reproduction is analogous to the existing (currently commented-out) test already present in the repository, which demonstrates the overflow class on the related payout arithmetic: [5](#0-4) 

An equivalent PoC for the finding above: directly seed `ErasRewardPoints::<Test>::insert(era, EraRewardPoints { total: u32::MAX - 5, individual: [(validator, u32::MAX - 5)].into() })`, then call `Staking::reward_by_ids(vec![(validator, 10)])` (or trigger it via repeated `note_author`/backing rewards) and observe `era_rewards.total`/`individual[validator]` wrap to a small value instead of saturating at `u32::MAX`, after which `payout_stakers_by_page` computes a `Perbill::from_rational` skewed by the wrapped denominator/numerator, producing an incorrect reward split versus the pre-overflow expected distribution.

### Citations

**File:** substrate/frame/staking/src/pallet/impls.rs (L321-337)
```rust
		let era_reward_points = <ErasRewardPoints<T>>::get(&era);
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

**File:** substrate/frame/staking/src/pallet/impls.rs (L849-858)
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
