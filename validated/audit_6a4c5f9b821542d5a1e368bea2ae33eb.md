Audit Report

## Title
Era reward marked as claimed before pot-transfer settlement succeeds, permanently losing staker payouts when the reward pot lacks balance - (File: substrate/frame/staking-async/src/pallet/impls.rs)

## Summary
`pallet-staking-async`'s non-minting reward mode pays stakers by transferring value out of a per-era reward pot rather than minting new tokens. The dispatch path `do_payout_stakers_by_page` marks `ClaimedRewards` via `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` before the per-payee transfers (`make_payout_from_provider`) are attempted, and a failed transfer only logs an error and skips the `Rewarded` event without reverting the extrinsic or resetting the claimed flag.

## Finding Description
`do_payout_stakers_by_page` checks `Eras::<T>::is_rewards_claimed` and then immediately calls `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` [1](#0-0)  well before the reward amounts are computed and the actual transfers are executed via `payout_from_provider`/`make_payout_from_provider` [2](#0-1) . `make_payout_from_provider` performs `T::Currency::transfer(&staker_rewards_pot, &payout_account, amount, Preservation::Expendable)`; on failure it logs an error and returns `None`, without surfacing the failure to the caller in any way that would revert the claimed flag or the extrinsic [3](#0-2) . Since `ClaimedRewards` was already updated, a retry attempt for the same era/page returns `Error::AlreadyClaimed` [4](#0-3) , permanently blocking recovery.

Separately, `EraRewardManager::drain` withdraws the full remaining era-pot balance to `UnclaimedRewardHandler` with no check for whether all payees of that era have already claimed [5](#0-4) . `set_rewards_as_claimed` itself is a straightforward defensive-guarded insert with no linkage to transfer success [6](#0-5) .

However, I could not locate the caller of `EraRewardManager::drain`/`cleanup_era` within the available index to confirm it is actually invoked automatically at `HistoryDepth` boundaries irrespective of outstanding unclaimed pages — the grep matches for `cleanup_era` in `reward.rs`, `session_rotation.rs`, and `tests/era_rotation.rs` were not resolved to a concrete call site within the tool budget, so the "drain races an unclaimed payee" half of the claim (step 3–4 of the PoC) is not independently verified against this repository's code, only the ordering bug in `do_payout_stakers_by_page` is directly confirmed.

Regardless of the drain-timing question, the core defect stands on its own: the "claimed" bookkeeping is updated unconditionally and prior to (and independent of) whether the transfer of funds actually succeeds. Any circumstance that leaves the era pot's reducible balance short of the amount owed for a given page — under-snapshotting in `snapshot_era_rewards` (which can zero out `actual_staker`/`actual_incentive` on transfer failure) [7](#0-6) , cumulative rounding/`Preservation::Expendable` reducible-balance shortfalls across many claimants of the same era, or an early drain — causes a payee's claim to be marked settled while no value moved, with no retry path.

## Impact Explanation
This directly violates "staking … reward payouts must conserve value and settle exactly once to the rightful beneficiary and amount" from the Polkadot SDK pivots: `ClaimedRewards` advances to a settled state without the underlying `Currency::transfer` having succeeded, permanently and silently losing the affected staker's payout (the extrinsic still returns `Ok`). This is a duplicate-settlement/permanent-fund-lock-class defect: value promised via `ErasValidatorReward`/`ErasRewardPoints` is never delivered, and the corrupted value is the `ClaimedRewards` (via `set_rewards_as_claimed`) entry for `(era, stash, page)`, which becomes permanently out of sync with actual fund movement.

## Likelihood Explanation
No malicious actor or privileged action is required — this is a straightforward public-dispatch (`payout_stakers`) sequencing defect reachable by any account calling the existing public extrinsic once the era pot's reducible balance is insufficient for a given page's payout. Insufficiency can arise from ordinary rounding/Expendable-preservation shortfalls or from the under-snapshotting logged in `snapshot_era_rewards`; the drain-race portion of the scenario remains unverified from available code in this session.

## Recommendation
Perform the per-payee transfer(s) for a page before calling `set_rewards_as_claimed`, and only persist the claimed flag if the transfer(s) succeed; otherwise return an error that leaves the page retryable. Additionally, ensure any pot-draining logic (`EraRewardManager::drain`/cleanup routines) cannot reclaim an era pot's balance while unclaimed pages remain for that era.

## Proof of Concept
1. Configure a staking-async runtime with `DisableMintingGuard` active (non-minting/DAP payout mode).
2. Arrange for the era pot's reducible balance to be less than the amount owed for a validator's page (e.g., via rounding across multiple pages/nominators, or a failed/partial `snapshot_era_rewards` transfer).
3. Call `payout_stakers` for that era/page: `do_payout_stakers_by_page` executes `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` at [8](#0-7)  then calls `make_payout_from_provider`, whose `T::Currency::transfer` fails and returns `None` at [9](#0-8) , yet the extrinsic still returns `Ok`.
4. No `Rewarded` event is emitted for the affected payee, and the reward is lost.
5. Re-invoke `payout_stakers` for the same era/page/stash — it returns `Error::AlreadyClaimed`, confirming no retry path exists.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-386)
```rust
		if Eras::<T>::is_rewards_claimed(era, &stash, page) {
			return Err(Error::<T>::AlreadyClaimed
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)));
		}

		Eras::<T>::set_rewards_as_claimed(era, &stash, page);
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

**File:** substrate/frame/staking-async/src/reward.rs (L106-120)
```rust
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

**File:** substrate/frame/staking-async/src/session_rotation.rs (L227-246)
```rust
	/// Creates an entry to track validator reward has been claimed for a given era and page.
	/// Noop if already claimed.
	pub(crate) fn set_rewards_as_claimed(era: EraIndex, validator: &T::AccountId, page: Page) {
		let mut claimed_pages = ClaimedRewards::<T>::get(era, validator).into_inner();

		// this should never be called if the reward has already been claimed
		if claimed_pages.contains(&page) {
			defensive!("Trying to set an already claimed reward");
			// nevertheless don't do anything since the page already exist in claimed rewards.
			return;
		}

		// add page to claimed entries
		claimed_pages.push(page);
		ClaimedRewards::<T>::insert(
			era,
			validator,
			WeakBoundedVec::<_, _>::force_from(claimed_pages, Some("set_rewards_as_claimed")),
		);
	}
```
