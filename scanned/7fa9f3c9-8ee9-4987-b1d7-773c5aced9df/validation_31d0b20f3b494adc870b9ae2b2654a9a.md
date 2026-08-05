### Title
Era reward marked as claimed before pot-transfer settlement succeeds, permanently losing staker payouts when the reward pot lacks balance - (File: substrate/frame/staking-async/src/pallet/impls.rs)

### Summary
`pallet-staking-async`'s non-minting reward mode pays stakers by transferring value out of a per-era reward pot instead of minting new tokens. The dispatch path marks a validator/page as `rewards_claimed` **before** it knows whether the underlying `Currency::transfer` from the pot actually succeeds. If the era pot does not hold enough balance to cover every payee (e.g. because it was already drained by the `HistoryDepth` cleanup routine, or because of `Preservation::Expendable` reducible-balance shortfalls), the transfer silently fails, the event is skipped, but the "claimed" flag is never rolled back. The staker permanently loses the reward with no way to retry, because subsequent calls are rejected with `AlreadyClaimed`. This is the same broken invariant as the reported bug: the protocol records/promises a payout without guaranteeing the backing balance actually exists, and settlement state is allowed to advance independent of whether funds were actually delivered.

### Finding Description
In non-minting mode (`DisableMinting = true`), rewards for an era are snapshotted from a general pot into a rotating per-era pot account by `EraRewardManager::snapshot_era_rewards`: [1](#0-0) 

`ErasValidatorReward`/reward points then determine how much each validator/nominator is *owed*, and the actual transfer of that owed amount happens per-payee via `make_payout_from_provider`, which transfers from the era pot to the payout account: [2](#0-1) 

Critically, in `do_payout_stakers_by_page`, the pallet marks the page as claimed with `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` **before** it computes and executes the individual transfers for the validator and each nominator on that page: [3](#0-2) 

The subsequent per-payee transfer (`make_payout_from_provider`) can fail (e.g., insufficient reducible balance in the era pot) and, on failure, only logs an error and returns `None` — it does not revert the extrinsic, nor does it unset the "claimed" flag: [4](#0-3) 

Meanwhile, the era pot's remaining balance can be forcibly withdrawn and handed to `UnclaimedRewardHandler` once the era passes `HistoryDepth`, via `EraRewardManager::drain`/`cleanup_era`, regardless of whether every payee has actually claimed their page yet: [5](#0-4) 

The net effect: `ErasValidatorReward`/exposure data promise a specific amount to every nominator/validator for an era, but nothing enforces that the pot's actual balance is sufficient to cover all of them at claim time, nor does the "claimed" bookkeeping stay in sync with whether the transfer actually landed. This is structurally identical to the `NFTLootbox` bug: state that represents "prize is owed/claimable" is decoupled from a guarantee that the contract/pallet actually holds the funds, so once the promise is recorded, the payer can end up insolvent for at least one payee, and that payee's claim right is destroyed regardless (since re-claiming returns `Error::AlreadyClaimed`).

### Impact Explanation
A staker (validator or nominator) whose page/era claim happens to be processed when the era pot is short on funds (pool already drained by `cleanup_era`, or partially exhausted because earlier claimants in the same era already consumed the reducible balance under `Preservation::Expendable`) permanently and silently loses their reward. The extrinsic still returns `Ok`, giving no indication of failure, and the claim can never be retried. This directly violates the required invariant that "staking... reward payouts must conserve value and settle exactly once to the rightful beneficiary and amount" — here it can settle to nobody while still being marked settled.

### Likelihood Explanation
This does not require a malicious actor. It occurs from ordinary state races: unclaimed reward cleanup (`cleanup_era`, triggered automatically past `HistoryDepth`) versus late claims for the same era, or partial exhaustion of the era pot's reducible balance across many claimants of the same era (each transfer uses `Preservation::Expendable`, so imprecision/rounding or a pot that was under-snapshotted can leave the last claimants unpaid). No privileged/governance action, no malicious peer/validator, and no off-chain assumption is needed — it is a straightforward public-dispatch (`payout_stakers`) sequencing bug.

### Recommendation
Do not mark `set_rewards_as_claimed` until after the transfer(s) for that page have been attempted and their outcome is known. Either:
- Perform the transfer(s) first and only set the claimed flag if `Currency::transfer` succeeds for every payee on the page, returning an error (and leaving the claim retryable) otherwise; or
- Track transfer failures explicitly and expose a `retry_payout` path that resumes only the failed leg, rather than silently dropping funds; and
- Ensure `EraRewardManager::drain`/`cleanup_era` cannot reclaim an era pot's balance while there are still unclaimed pages for that era (e.g. track an `unclaimed pages` counter per era and gate `drain` on it reaching zero).

### Proof of Concept
1. Configure a staking-async runtime in non-minting mode (`DisableMinting = true`).
2. Let an era complete; `snapshot_era_rewards` funds the era pot with `staker_rewards` sufficient to cover all recorded reward points.
3. Advance chain state so the era falls at the edge of `HistoryDepth`; trigger `cleanup_era`/`drain` for that era before all validators/nominators have called `payout_stakers` for every page (this can legitimately happen if some stakers are slow to claim).
4. `drain` withdraws the era pot's full remaining balance into `UnclaimedRewardHandler`, even though other stakers still have an outstanding, valid claim for that era.
5. A staker who has not yet claimed calls `payout_stakers` for their page. `do_payout_stakers_by_page` marks `set_rewards_as_claimed` immediately, then `make_payout_from_provider` attempts to transfer from the now-empty era pot and fails, logging an error but returning `Ok(())` from the extrinsic.
6. The staker's reward is permanently lost; re-invoking `payout_stakers` for the same era/page now returns `Error::AlreadyClaimed`, confirming there is no path to recovery.

### Citations

**File:** substrate/frame/staking-async/src/reward.rs (L84-125)
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
```

**File:** substrate/frame/staking-async/src/reward.rs (L156-203)
```rust
	/// Drains an era pot's remaining balance to the unclaimed reward handler.
	///
	/// The pot account itself is kept alive (provider retained) so the same slot
	/// can be reused by a future era. No-op if the pot was never created (e.g.
	/// the era ran in legacy minting mode).
	pub(crate) fn drain(era: EraIndex, kind: RewardKind) {
		let pot_account = T::RewardPots::pot_account(RewardPot::Era(era, kind));

		// Skip if pot was never created (legacy mode doesn't create pots).
		if frame_system::Pallet::<T>::providers(&pot_account) == 0 {
			return;
		}

		let remaining = T::Currency::balance(&pot_account);

		if remaining.is_zero() {
			return;
		}

		match T::Currency::withdraw(
			&pot_account,
			remaining,
			Precision::BestEffort,
			Preservation::Expendable,
			Fortitude::Force,
		) {
			Ok(credit) => {
				T::UnclaimedRewardHandler::on_unbalanced(credit);
				log!(
					debug,
					"Drained {:?} unclaimed rewards from era {:?} {:?} pot",
					remaining,
					era,
					kind
				);
			},
			Err(e) => {
				defensive!("Failed to withdraw unclaimed rewards from era pot");
				log!(
					error,
					"Era {:?} {:?}: unclaimed reward withdrawal failed: {:?}",
					era,
					kind,
					e
				);
			},
		}
	}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-401)
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

		// Input data seems good, no errors allowed after this point

		let era_reward_points = Eras::<T>::get_reward_points(era);
		let total_reward_points = era_reward_points.total;
		let validator_reward_points =
			era_reward_points.individual.get(&stash).copied().unwrap_or_else(Zero::zero);

		// Nothing to do if they have no reward points.
		if validator_reward_points.is_zero() {
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L577-616)
```rust
	/// Make a payment to a staker from an era reward pot (transfer, not mint).
	fn make_payout_from_provider(
		era: EraIndex,
		stash: &T::AccountId,
		amount: BalanceOf<T>,
	) -> Option<(BalanceOf<T>, RewardDestination<T::AccountId>)> {
		if amount.is_zero() {
			return None;
		}

		let dest = match Self::payee(Stash(stash.clone())) {
			Some(d) => d,
			None => {
				Self::deposit_event(Event::<T>::Unexpected(UnexpectedKind::MissingPayee {
					era,
					stash: stash.clone(),
				}));
				return None;
			},
		};

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
