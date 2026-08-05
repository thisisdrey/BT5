Audit Report

## Title
Staker reward payouts are silently dropped and unrecoverable when the pot-to-staker transfer fails - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

## Summary
In `pallet-staking-async`, `do_payout_stakers_by_page` marks an era/page as claimed via `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` before performing any actual transfers, then calls `payout_from_provider`, which pays the validator and each nominator via `make_payout_from_provider`. If the underlying `T::Currency::transfer` from the `staker_rewards_pot` to a staker's payout account fails, the error is only logged and the function returns `None` — the caller does not propagate the failure, the extrinsic still returns `Ok(..)`, and the page remains marked as claimed with no retry path.

## Finding Description
`do_payout_stakers_by_page` sets the claimed-page bookkeeping unconditionally and early, prior to any transfer: [1](#0-0) . It then computes the payout split and calls `payout_from_provider` for the non-legacy path: [2](#0-1) .

`payout_from_provider` iterates the validator and every nominator, calling `make_payout_from_provider` for each and only emitting a `Rewarded` event and incrementing the payout counter on success — a failed transfer is simply skipped with no error surfaced: [3](#0-2) .

`make_payout_from_provider` performs the actual settlement from the per-era `staker_rewards_pot` and, on transfer failure, only logs the error and returns `None`: [4](#0-3) . Since the page was already marked claimed before this call, and the enclosing dispatch always returns `Ok(Some(weight).into())` regardless of any individual transfer failures, there is no mechanism to retry that staker's payout for that era/page.

I also examined how the `staker_rewards_pot` is funded, via `EraRewardManager::snapshot_era_rewards`, which transfers the general pot's reducible balance (preserving ED) into the era-specific pot at era end: [5](#0-4) . This funding transfer itself can also fail and is swallowed with only a `defensive!` log, potentially leaving the era pot underfunded relative to the nominal `era_payout` used in reward calculations, which increases the reachability of the downstream per-staker transfer failing under `Preservation::Expendable`. Destination-side existential-deposit edge cases (e.g., a `RewardDestination::Account` pointing to a reaped/dust account) are also a plausible, non-privileged trigger for `make_payout_from_provider`'s transfer to fail.

## Impact Explanation
This matches the "permanent user-fund ... lock" / lost-value category: a staker's (validator's or nominator's) era reward can be permanently and silently lost because (1) the claimed-page state is set before settlement, and (2) individual transfer failures inside the payout loop are absorbed without propagating an error or reverting the claimed status. The value remains stranded in the untouched `staker_rewards_pot` for that era, and once the era falls outside `HistoryDepth`, `EraRewardManager::drain` sweeps the pot's remaining balance to `UnclaimedRewardHandler`, permanently removing any possibility of the staker recovering their exact entitlement. The exact corrupted state is the `RewardsClaimed`/claimed-page bookkeeping for `(era, stash, page)` being marked complete despite an incomplete/failed transfer to the affected staker's payout account.

## Likelihood Explanation
`payout_stakers` is a public, unprivileged extrinsic that can be called by anyone for any validator/era/page. Triggering a transfer failure requires either (a) the era's `staker_rewards_pot` being underfunded (itself reachable via a swallowed failure in `snapshot_era_rewards`, or normal rounding/dust behavior), or (b) a destination account for a specific staker hitting an existential-deposit edge case under `Preservation::Expendable`. Neither requires a malicious validator, collator, or privileged actor — it is a naturally reachable state given per-account balance conditions, consistent with the programs' "permanent user-fund lock" acceptance criteria rather than the excluded malicious-actor categories.

## Recommendation
1. Do not call `Eras::<T>::set_rewards_as_claimed` until after settlement of all individual payouts in the page has been confirmed, or
2. Persist a per-staker "unpaid/failed reward" ledger entry when `make_payout_from_provider` fails, exposing a dedicated retry/reclaim extrinsic, instead of only logging the error and firing a best-effort event with no remediation path.
3. Apply the same fix to `make_payout_legacy`, which has the identical swallow-on-error pattern for the legacy mint path.

## Proof of Concept
1. Configure an era with a `staker_rewards_pot` and a validator exposure with several nominators.
2. Underfund the `staker_rewards_pot` relative to `total_nominator_payout` (e.g., by causing `snapshot_era_rewards`'s inner transfer to under-deliver via ED preservation, or by engineering dust/rounding across `mul_floor` splits) or set one nominator's `payee` to `RewardDestination::Account` pointing at an account below ED.
3. Call `payout_stakers(origin, validator_stash, era)` from any unprivileged account.
4. Observe the call returns `Ok(..)`, `Eras::<T>::is_rewards_claimed(era, &stash, page)` is `true`, but not all expected `Rewarded` events were emitted — the affected nominator's balance is unchanged and the page can never be reprocessed via `get_next_claimable_page`, confirming the reward is permanently lost.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-386)
```rust
		if Eras::<T>::is_rewards_claimed(era, &stash, page) {
			return Err(Error::<T>::AlreadyClaimed
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)));
		}

		Eras::<T>::set_rewards_as_claimed(era, &stash, page);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L451-478)
```rust
		// Determine whether to use dap payout or legacy path.
		let use_dap_payout =
			DisableMintingGuard::<T>::get().is_some_and(|guard_era| era >= guard_era);

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

		debug_assert!(nominator_payout_count <= T::MaxExposurePageSize::get());

		Ok(Some(T::WeightInfo::payout_stakers_alive_staked(nominator_payout_count)).into())
	}
```

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

**File:** substrate/frame/staking-async/src/reward.rs (L84-153)
```rust
	/// Snapshots the general reward pots into era-specific pots.
	///
	/// DAP drips inflation continuously into the general pots. At era boundary,
	/// this transfers the accumulated balances (minus ED) into era pots.
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
