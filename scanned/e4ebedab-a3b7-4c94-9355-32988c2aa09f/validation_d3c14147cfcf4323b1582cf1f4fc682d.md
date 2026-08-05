## Title
Nominator/validator reward payout is marked as claimed *before* the transfer succeeds, permanently forfeiting rewards on legitimate transfer failure - (File: `substrate/frame/staking-async/src/pallet/impls.rs`)

### Summary
The Solidity report's core invariant break is: **state advances (or a call is treated as done) even though the underlying value transfer can silently fail**, causing funds to become unreachable/lost. The direct on-chain analog is `Pallet::<T>::do_payout_stakers_by_page` in `pallet-staking-async`, which marks an era/page as `rewards_claimed` before any reward is actually transferred, and then swallows `Currency::transfer` failures inside `make_payout_from_provider` with only a `log::error!`, never rolling back the "claimed" flag.

### Finding Description
`do_payout_stakers_by_page` sets the claim marker immediately after the double-claim check and before doing any payout: [1](#0-0) 

It then calls `payout_from_provider`, which in turn calls `make_payout_from_provider` for the validator and each nominator: [2](#0-1) 

`make_payout_from_provider` performs the actual value movement with `T::Currency::transfer(..., Preservation::Expendable)`. If the transfer errors (e.g. destination account would end up below the Existential Deposit and cannot be created/kept alive, or the era reward pot is short due to `Perbill` rounding across many nominators in a page), the function only logs the error and returns `None` — the reward is simply dropped, no event is emitted, and crucially **the page has already been marked as claimed** back in `do_payout_stakers_by_page`: [3](#0-2) 

Since `Eras::<T>::is_rewards_claimed(era, &stash, page)` now returns `true`, any unprivileged retry of `payout_stakers_by_page` for that era/page hits `Error::<T>::AlreadyClaimed` (this guard and the double-claim tests confirm no retry is possible): [4](#0-3) 

Notably, the codebase treats an analogous failure for the *validator incentive* pot as a fatal defensive condition that panics the runtime (`defensive!("Validator incentive liquid transfer failed")`), showing the developers recognize a failed reward transfer is a serious invariant violation — yet the ordinary nominator/validator staking-reward path in `make_payout_from_provider` handles the identical failure mode by silently discarding the payment instead: [5](#0-4) [6](#0-5) 

This is the exact bug class of the external report translated to Substrate: a value transfer that can fail is not properly guarded, and the "already handled" bookkeeping (claimed flag / event) is committed regardless of whether the transfer actually delivered funds, so the recipient permanently loses the payout with no recourse.

### Impact Explanation
Any staker (validator payee or nominator) whose reward-destination account is dust-level or would fall below ED after receiving a small reward, or any page whose total transferable amount is reduced by `Perbill` rounding so the era reward pot underpays by a negligible amount, will have their reward silently and permanently lost: the era/page is marked claimed, no `Rewarded` event fires, and no further call can re-trigger payment for that stash/era/page. This is a permanent user-fund loss/lock triggered without any privileged actor, malicious peer, or governance action — purely through the reward-calculation/transfer flow itself, which is explicitly in-scope under "permanent user-fund or bridge-state lock" and "duplicate settlement or payout" impacts.

### Likelihood Explanation
No attacker action is required — this can occur naturally: nominators frequently receive very small per-era rewards (large nominator sets split rewards across many low-stake accounts), and `Perbill::mul_floor` rounding on many nominators in a page compounds. Any nominator whose payout account is dormant/reaped and whose reward transfer would leave it below ED, or whose pot underflows to leave the last recipient in the page unpaid, hits this path deterministically the moment `payout_stakers_by_page`/`payout_stakers` is dispatched by anyone (dispatch is unsigned-caller-agnostic — see `ensure_signed(origin)?` only).

### Recommendation
Move `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` to after all transfers in the page have been attempted, or track per-recipient claim status so failed transfers can be retried/refunded, and propagate `make_payout_from_provider`/`payout_from_provider` transfer failures into either a partial-success event or an explicit failure path (mirroring the `defensive!`-guarded handling already used for `transfer_validator_incentive`) rather than silently discarding them.

### Proof of Concept
1. Set up an era with a validator and a nominator whose payout account has zero balance and reward destination `Account(fresh_low_ed_account)`.
2. Arrange the nominator's computed `nominator_reward` (via `Perbill::from_rational(...).mul_floor(...)`) to be below `ExistentialDeposit`.
3. Call `Staking::payout_stakers_by_page(origin, validator_stash, era, page)` as any signed account.
4. Observe: `Eras::<T>::set_rewards_as_claimed` has already run; `make_payout_from_provider`'s `T::Currency::transfer` fails (dest below ED) and only logs `error!("Failed to transfer reward from pot ...")`; no `Rewarded` event for the nominator; a follow-up call to `payout_stakers_by_page` for the same era/page returns `Error::<T>::AlreadyClaimed` — the reward is permanently unrecoverable.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-393)
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L577-617)
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L763-802)
```rust
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
```

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L2827-2837)
```rust
		#[pallet::call_index(26)]
		#[pallet::weight(T::WeightInfo::payout_stakers_alive_staked(T::MaxExposurePageSize::get()))]
		pub fn payout_stakers_by_page(
			origin: OriginFor<T>,
			validator_stash: T::AccountId,
			era: EraIndex,
			page: Page,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			Self::do_payout_stakers_by_page(validator_stash, era, page)
		}
```

**File:** substrate/frame/staking-async/src/tests/validator_incentive.rs (L710-741)
```rust
#[test]
#[should_panic(expected = "Validator incentive liquid transfer failed")]
fn defensive_panic_on_transfer_failure() {
	ExtBuilder::default().build_and_execute(|| {
		let alice = 11; // validator

		// GIVEN: incentive enabled, validator has weight.
		setup_incentive_with_budget(45, 5);
		Session::roll_until_active_era(2);
		Eras::<Test>::reward_active_era(vec![(alice, 1), (21, 1)]);
		Session::roll_until_active_era(3);

		// WHEN: drain the incentive pot so transfer fails.
		let pot = <Test as Config>::RewardPots::pot_account(RewardPot::Era(
			2,
			RewardKind::ValidatorSelfStake,
		));
		let pot_balance = Balances::free_balance(&pot);
		if pot_balance > 0 {
			// Transfer everything out to account 999 to empty the pot.
			let _ = <Balances as frame_support::traits::fungible::Mutate<_>>::transfer(
				&pot,
				&999,
				pot_balance,
				frame_support::traits::tokens::Preservation::Expendable,
			);
		}

		// THEN: payout panics on defensive.
		make_all_reward_payment(2);
	});
}
```
