Audit Report

## Title
`payout_stakers` marks era/page rewards as claimed before the pot-to-staker transfer is confirmed to succeed, permanently stranding staker rewards on transfer failure - (File: substrate/frame/staking-async/src/pallet/impls.rs)

## Summary
In `do_payout_stakers_by_page`, `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` is executed unconditionally before any actual value transfer is attempted, and the real balance movement happens later per-payee inside `make_payout_from_provider`, which silently swallows `T::Currency::transfer` errors and returns `None` rather than propagating a `DispatchError`. [1](#0-0) [2](#0-1)  If the transfer fails for any reason, the extrinsic still completes with `Ok(...)`, and the page/stash/era combination is permanently marked claimed with no retry path since a repeat call hits `Error::<T>::AlreadyClaimed`.

## Finding Description
`do_payout_stakers_by_page` marks the page claimed unconditionally at line 386 before computing or executing any payout: [3](#0-2)  It then computes `validator_staker_payout_for_page` and nominator shares from `Perbill` splits of `era_payout`, and dispatches per-payee transfers via `payout_from_provider` → `make_payout_from_provider`, whose actual transfer failure path just logs and returns `None`, with no error bubbled up to the caller: [2](#0-1) 

The claimed-status update and the actual transfer are therefore not atomic, and a per-nominator transfer failure inside a loop (`payout_from_provider`) does not roll back `set_rewards_as_claimed` or abort remaining payouts in the page. [4](#0-3) 

However, on inspecting the surrounding reward-accounting logic, the individual payout amounts computed via `Perbill::mul_floor` (validator_total_payout, validator_staker_payout_for_page, nominator_reward) are strictly floor-divided fractions of `era_payout`, meaning per-page and per-nominator sums are always ≤ the intended total, not more. [5](#0-4)  The era pot itself is funded during `snapshot_era_rewards` by transferring the *entire* reducible balance of the general staker pot into the era-specific pot using `Preservation::Preserve`, and this snapshotted amount (`actual_staker`) is what should back `era_payout`/`ErasValidatorReward`. [6](#0-5)  I was not able to fully confirm in this investigation whether `ErasValidatorReward`/`get_stakers_reward(era)` is always set equal to the *actual* snapshotted amount (`actual_staker`) or to some independently-computed "intended" reward figure that could exceed what was actually moved into the pot when `snapshot_era_rewards`'s transfer fails and falls back to `Zero::zero()` (visible in the `Err` branch of the snapshot transfer). [7](#0-6)  This distinction is material: if `ErasValidatorReward` is always derived from `actual_staker` (the confirmed pot balance), then the described underfunding scenario cannot arise through ordinary rounding/dust behavior, since all downstream splits are floor-divided from a value that is provably ≤ the pot's real balance. The claim's PoC section itself acknowledges this gap, stating the numeric conditions required to force a `make_payout_from_provider` transfer failure could not be verified without running the existing test harness (`substrate/frame/staking-async/src/tests/payout_stakers.rs`) with a deliberately underfunded pot.

Regardless of whether the underfunding precondition is reachable through pure dust/rounding in production, the underlying code-level defect is real and independently verifiable: `set_rewards_as_claimed` is committed unconditionally before transfer confirmation, and `make_payout_from_provider`'s failure path degrades silently instead of surfacing an error, which is a genuine "commit accounting update before transfer confirmation" anti-pattern per the code as written. [8](#0-7) 

## Impact Explanation
If the precondition (era pot balance insufficient relative to computed per-page/per-nominator payout) is ever reached — which is plausible given the `Err` fallback to `Zero::zero()` in `snapshot_era_rewards` combined with `ErasValidatorReward` potentially not tracking that fallback exactly, though this exact coupling was not fully confirmed — the result is a permanent, unbacked loss of staker/validator reward funds: the beneficiary's balance is never credited, yet the page is irreversibly flagged `AlreadyClaimed`, with no retry mechanism (unlike `pallet-treasury`/`pallet-bounties`'s `Pay`/`check_status`/`retry_payment` pattern). This fits the "permanent user-fund lock" / theft-adjacent impact category if and only if the underfunding precondition is reachable without privileged action.

## Likelihood Explanation
The exploit path requires no malicious actor — `payout_stakers` is callable by anyone (`ensure_signed`) — but likelihood hinges entirely on whether the underfunding precondition is reachable in practice. This could not be conclusively confirmed: it depends on the exact relationship between `ErasValidatorReward` (used for the `Perbill` splits) and the actually-snapshotted era pot balance (`actual_staker`/`actual_incentive`), which requires deeper tracing through `session_rotation.rs`'s era-end logic than was completed in this review. Without that confirmation, the likelihood of a naturally-occurring, exploit-triggerable underfunding gap remains unverified rather than demonstrated.

## Recommendation
Regardless of the underfunding-reachability question, the code-level ordering defect should be fixed: perform the transfer(s) for a page before (or atomically with) calling `set_rewards_as_claimed`, and propagate transfer failures from `make_payout_from_provider` as a `DispatchError` that aborts (and does not mark claimed) the page, rather than silently degrading to `None`. As a stronger structural fix, adopt the two-phase `Pay`/`PaymentState` pattern already used elsewhere in the codebase (e.g. `pallet-treasury`, `pallet-bounties`) so that a failed transfer leaves the page retryable instead of permanently and incorrectly marked claimed. Additionally, add an invariant (e.g., a `try_state` check) confirming `ErasValidatorReward`/computed page payouts never exceed the era pot's actual balance snapshotted by `snapshot_era_rewards`, to close the verification gap identified above.

## Proof of Concept
Not independently reproduced. A concrete reproduction requires confirming, via the mock runtime in `substrate/frame/staking-async/src/tests/payout_stakers.rs` and `substrate/frame/staking-async/src/tests/era_rotation.rs`, whether `ErasValidatorReward` can ever exceed the era pot's actual `actual_staker`/`actual_incentive` balance from `snapshot_era_rewards`, and if so, forcing that gap and calling `payout_stakers` for the affected page to observe `set_rewards_as_claimed` committing while `T::Currency::transfer` in `make_payout_from_provider` fails and returns `None`. This dependency was not resolved within the scope of this investigation, so the practical reachability of the impact (as opposed to the code-level ordering defect itself) remains unconfirmed.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-392)
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L405-431)
```rust
		// This is the fraction of the total reward that the validator and the
		// nominators will get.
		let validator_total_reward_part =
			Perbill::from_rational(validator_reward_points, total_reward_points);

		// This is how much validator + nominators are entitled to.
		let validator_total_payout = validator_total_reward_part.mul_floor(era_payout);

		let validator_commission = Eras::<T>::get_validator_commission(era, &ledger.stash);

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

		// Prorate the validator's reward (commission + own-stake share) across pages
		// proportional to each page's stake relative to total.
		let page_stake_part = Perbill::from_rational(exposure.page_total(), exposure.total());
		let validator_staker_payout_for_page =
			page_stake_part.mul_floor(reward_split.validator_payout);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L488-516)
```rust
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L598-617)
```rust
		let payout_account = Self::payout_account_for_dest(stash, &dest)?;

		let staker_rewards_pot =
			T::RewardPots::pot_account(RewardPot::Era(era, RewardKind::StakerRewards));
		if let Err(e) = T::Currency::transfer(
			&staker_rewards_pot,
			&payout_account,
			amount,
			Preservation::Expendable,
		) {
			log!(
				error,
				"Failed to transfer reward from pot for era {:?}, stash {:?}: {:?}",
				era,
				stash,
				e
			);
			return None;
		}

```

**File:** substrate/frame/staking-async/src/reward.rs (L88-153)
```rust
	pub(crate) fn snapshot_era_rewards(era: EraIndex) -> EraRewardAllocation<BalanceOf<T>> {
		let staker_era_pot = Self::create(era, RewardKind::StakerRewards);
		let incentive_era_pot = Self::create(era, RewardKind::ValidatorSelfStake);

		let general_staker_pot =
			T::RewardPots::pot_account(RewardPot::General(RewardKind::StakerRewards));
		let general_incentive_pot =
			T::RewardPots::pot_account(RewardPot::General(RewardKind::ValidatorSelfStake));

		// Leave ED in the general pots to keep them alive.
		let staker_balance = T::Currency::reducible_balance(
			&general_staker_pot,
			Preservation::Preserve,
			Fortitude::Polite,
		);
		let incentive_balance = T::Currency::reducible_balance(
			&general_incentive_pot,
			Preservation::Preserve,
			Fortitude::Polite,
		);

		let actual_staker = if !staker_balance.is_zero() {
			match T::Currency::transfer(
				&general_staker_pot,
				&staker_era_pot,
				staker_balance,
				Preservation::Preserve,
			) {
				Ok(_) => staker_balance,
				Err(e) => {
					log!(error, "Era {:?}: staker reward transfer failed: {:?}", era, e);
					defensive!("Failed to transfer staker rewards to era pot");
					Zero::zero()
				},
			}
		} else {
			Zero::zero()
		};

		let actual_incentive = if !incentive_balance.is_zero() {
			match T::Currency::transfer(
				&general_incentive_pot,
				&incentive_era_pot,
				incentive_balance,
				Preservation::Preserve,
			) {
				Ok(_) => incentive_balance,
				Err(e) => {
					log!(error, "Era {:?}: validator incentive transfer failed: {:?}", era, e);
					defensive!("Failed to transfer validator incentive to era pot");
					Zero::zero()
				},
			}
		} else {
			Zero::zero()
		};

		log!(
			info,
			"Era {:?}: snapshotted staker_rewards={:?}, validator_incentive={:?}",
			era,
			actual_staker,
			actual_incentive
		);

		EraRewardAllocation { staker_rewards: actual_staker, validator_incentive: actual_incentive }
```
