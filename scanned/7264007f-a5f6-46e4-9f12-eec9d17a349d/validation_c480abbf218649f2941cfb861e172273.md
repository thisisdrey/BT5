## Title
Staker rewards are marked as claimed before the pot-to-staker transfer succeeds, permanently stranding funds on transfer failure - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

## Summary
This is a direct structural analog of the Roots bug: an accounting flag ("claimed"/"surplus recorded") is set as if the payout has completed, while the actual value transfer is performed afterward from a separate custody location (a reward pot account) that is not guaranteed to still hold the funds. In Roots, `surplusBalances` is recorded but the collateral actually lives in the `Staker` contract, so `claimCollateral` reverts. In `pallet-staking-async`, `Eras::<T>::set_rewards_as_claimed` is executed *before* the reward is actually moved out of the era-specific reward pot via `make_payout_from_provider`. If that transfer fails, the code does not revert, does not re-open the claim, and does not fail the extrinsic — the reward is permanently lost with no path to retry.

## Finding Description
`do_payout_stakers_by_page` sets the "claimed" flag unconditionally and only afterward attempts the actual transfer: [1](#0-0) 

The nominator/validator payout is then performed by `payout_from_provider`, which calls `make_payout_from_provider` for each entry: [2](#0-1) 

`make_payout_from_provider` performs a plain `T::Currency::transfer` from the era reward pot to the payout account. Crucially, on failure it only logs and emits an informational `Unexpected` event, then returns `None` — it does not propagate an error, does not roll back `set_rewards_as_claimed`, and does not cause the surrounding extrinsic to fail: [3](#0-2) 

The same fire-and-forget pattern exists for the validator self-stake incentive pot in `transfer_validator_incentive`, which is likewise called unconditionally as part of the same payout path and also only logs+ event on failure without reverting: [4](#0-3) [5](#0-4) 

Because `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` is already persisted before any transfer is attempted, and `do_payout_stakers_by_page` returns `Ok(...)` regardless of whether the inner transfer succeeded, there is no mechanism to retry: subsequent calls to `payout_stakers`/`payout_stakers_by_page` for that `(era, stash, page)` will hit the `AlreadyClaimed` guard: [6](#0-5) 

This mirrors the Roots root cause exactly: the pallet's internal bookkeeping ("this has been paid out") is decoupled from the actual custody state of the funds (a separate pot account whose balance can legitimately be insufficient — e.g., depleted by cumulative rounding across a large exposure page, an unusually small snapshot from the general DAP pot for that era, or a pot that has already been drained by `EraRewardManager::drain` due to `HistoryDepth`/pot-slot-rotation edge cases described in the pallet's own `POT_POOL_SIZE` invariant comments): [7](#0-6) 

Unlike the legacy minting path (`make_payout_legacy`), which always succeeds because it mints tokens directly into existence, the new non-minting/DAP path introduces a genuine transfer-can-fail dependency that the "claimed" bookkeeping does not account for.

## Impact Explanation
A staker (nominator or validator) whose reward transfer fails for any reason permanently loses that era's reward with no recourse — the `AlreadyClaimed` flag is set regardless of transfer outcome, so the reward can never be claimed again. This is a "permanent user-fund lock/loss" impact directly in scope of the program (theft/unbacked mint or unlock, duplicate settlement, or permanent fund lock class), reachable through the completely public, unprivileged `payout_stakers`/`payout_stakers_by_page` extrinsics.

## Likelihood Explanation
Likelihood is Medium: the failure requires the era reward pot's actual balance to be insufficient at the moment an individual nominator/validator's share is transferred (e.g., due to rounding accumulation over many entries in a large exposure page, or a pot that was drained/reused due to the rotating `POT_POOL_SIZE` pot-slot scheme colliding with `HistoryDepth`). This does not require a malicious validator, collator, relayer, or governance actor — any ordinary payout call triggered by any signed account can hit this path once the pot balance is exhausted.

## Recommendation
Do not call `Eras::<T>::set_rewards_as_claimed` until after all transfers for that `(era, stash, page)` have been confirmed successful. Propagate transfer errors from `make_payout_from_provider`/`transfer_validator_incentive` up through `payout_from_provider`/`do_payout_stakers_by_page` so the whole extrinsic fails atomically (or explicitly track and allow retry of the specific failed sub-payment) instead of silently dropping funds while marking the claim as settled.

## Proof of Concept
1. Configure a runtime in DAP (non-minting) mode with `DisableMinting = true`.
2. Let an era accrue reward points for a validator with many nominators such that per-nominator payouts sum, after floor-rounding, close to the exact era pot balance (or otherwise arrange for the era pot's balance to be reduced below the expected payout, e.g. via `EraRewardManager::drain` firing prematurely due to pot-slot reuse).
3. Call `payout_stakers_by_page(validator, era, page)`. `Eras::<T>::set_rewards_as_claimed` is written immediately (`impls.rs:386`).
4. During iteration in `payout_from_provider`, one nominator's `make_payout_from_provider` transfer fails because the pot is depleted (`impls.rs:602-616`); the error is only logged/eventing, and the loop continues.
5. The extrinsic returns `Ok(...)`. That nominator's reward for `(era, page)` was never delivered.
6. Any later attempt to re-claim `payout_stakers_by_page(validator, era, page)` fails with `Error::<T>::AlreadyClaimed` (checked at `impls.rs:381-384`), permanently stranding the unpaid reward.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-386)
```rust
		if Eras::<T>::is_rewards_claimed(era, &stash, page) {
			return Err(Error::<T>::AlreadyClaimed
				.with_weight(T::WeightInfo::payout_stakers_alive_staked(0)));
		}

		Eras::<T>::set_rewards_as_claimed(era, &stash, page);
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L440-449)
```rust
		// Pay validator incentive bonus from the separate incentive pot.
		// Emits `ValidatorIncentivePaid` event inside `transfer_validator_incentive`.
		if let Some(incentive) = Self::calculate_validator_incentive_for_page(
			era,
			&stash,
			page_stake_part,
			&era_reward_points,
		) {
			Self::transfer_validator_incentive(era, &stash, incentive);
		}
```

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L481-516)
```rust
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L760-803)
```rust
	/// Transfer validator incentive from era pot to the validator's payout account.
	///
	/// This is a direct liquid transfer. Future PRs may introduce vesting via a trait.
	fn transfer_validator_incentive(era: EraIndex, stash: &T::AccountId, amount: BalanceOf<T>) {
		let Some(dest) = Self::payee(Stash(stash.clone())) else {
			Self::deposit_event(Event::<T>::Unexpected(UnexpectedKind::MissingPayee {
				era,
				stash: stash.clone(),
			}));
			return;
		};
		let Some(payout_account) = Self::payout_account_for_dest(stash, &dest) else {
			// Destination is `None`; intentional opt-out.
			return;
		};

		let incentive_pot = T::RewardPots::pot_account(crate::RewardPot::Era(
			era,
			crate::RewardKind::ValidatorSelfStake,
		));

		match T::Currency::transfer(
			&incentive_pot,
			&payout_account,
			amount,
			Preservation::Expendable,
		) {
			Ok(_) => {
				Self::deposit_event(Event::<T>::ValidatorIncentivePaid {
					era,
					validator_stash: stash.clone(),
					dest,
					amount,
				});
			},
			Err(e) => {
				log!(warn, "Failed to transfer liquid incentive: {:?}", e);
				Self::deposit_event(Event::<T>::Unexpected(
					UnexpectedKind::ValidatorIncentiveTransferFailed { era },
				));
				defensive!("Validator incentive liquid transfer failed");
			},
		}
	}
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
