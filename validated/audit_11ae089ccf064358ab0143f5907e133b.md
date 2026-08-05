Audit Report

## Title
Nominator/validator reward payout is marked as claimed before the transfer succeeds, permanently forfeiting rewards on legitimate transfer failure - (File: substrate/frame/staking-async/src/pallet/impls.rs)

## Summary
`do_payout_stakers_by_page` calls `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` immediately after the double-claim check and before any value is transferred [1](#0-0) . The actual transfer happens later in `make_payout_from_provider`, which on `T::Currency::transfer` failure only logs an error and returns `None`, silently discarding the reward with no rollback of the claimed flag and no event emission [2](#0-1) .

## Finding Description
The claimed-status marker is committed unconditionally before payout is attempted: `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` runs right after the `is_rewards_claimed` check and before `get_paged_exposure`/reward computation even occurs [1](#0-0) .

`payout_from_provider` then iterates the validator and each nominator, calling `make_payout_from_provider` for each and only emitting a `Rewarded` event on success (`if let Some(...) = ...`) [3](#0-2) . Inside `make_payout_from_provider`, the transfer from the era's `staker_rewards_pot` to the `payout_account` uses `Preservation::Expendable`; if it errors (e.g., destination below Existential Deposit, or pot underfunded due to `Perbill::mul_floor` rounding across a page of nominators), the function logs `log!(error, "Failed to transfer reward from pot ...")` and returns `None`, with no error propagated upward and no state rolled back [4](#0-3) .

Because `Eras::<T>::set_rewards_as_claimed` was already called before any transfer was attempted, a subsequent call to `payout_stakers_by_page` for the same `(era, stash, page)` deterministically fails with `Error::<T>::AlreadyClaimed` [5](#0-4) . This dispatchable is reachable by any signed (unprivileged) account via `ensure_signed(origin)?` in `payout_stakers_by_page`, so no privileged actor is needed to trigger the flow — only a payee whose destination account transfer would fail is needed [6](#0-5) .

The codebase's own handling of an analogous transfer, `transfer_validator_incentive`, treats a failed pot-to-payee transfer as a defensive/fatal condition (`defensive!("Validator incentive liquid transfer failed")`) rather than silently swallowing it, demonstrating that the developers recognize failed reward transfers are a serious invariant violation that the ordinary staking-reward path does not similarly guard against.

## Impact Explanation
This matches the "permanent user-fund lock" and "duplicate settlement or payout" impact categories: the claimed marker (settlement state) advances even though the underlying `Currency::transfer` can fail, and once advanced there is no code path to retry or recover the payout for that exact `(era, stash, page)` — the reward becomes permanently unreachable with the exact corrupted value being the `RewardsClaimed`/claimed-bitmap entry for that era/stash/page combination in `Eras::<T>` storage, decoupled from whether `payout_account` balance actually increased.

## Likelihood Explanation
No attacker privilege is required — `payout_stakers_by_page` is callable by any signed account against any validator/era/page [6](#0-5) . The failure condition (destination below ED, or Perbill rounding shortfall in the reward pot across many nominators) is a naturally occurring condition on real staking sets with dust-level payout accounts or large nominator pages, making this readily and repeatably triggerable without any malicious peer, governance, or compromised-key assumption.

## Recommendation
Move `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` to after all transfers for the page have been attempted and succeeded, or track claim status per-recipient so a failed transfer can be retried, and propagate `make_payout_from_provider` failures into an explicit failure/event path (mirroring the `defensive!`-guarded handling used in `transfer_validator_incentive`) instead of silently discarding the payout.

## Proof of Concept
1. Set up an era with a validator and a nominator whose payout account has zero balance and `RewardDestination::Account(fresh_low_ed_account)`.
2. Ensure the nominator's `Perbill::from_rational(...).mul_floor(...)`-computed reward is below `ExistentialDeposit`.
3. Call `Staking::payout_stakers_by_page(origin, validator_stash, era, page)` as any signed account.
4. Observe `Eras::<T>::set_rewards_as_claimed` already executed at [7](#0-6) ; the subsequent `T::Currency::transfer` in `make_payout_from_provider` fails and only logs an error at [8](#0-7) ; no `Rewarded` event is emitted for the nominator.
5. Re-call `payout_stakers_by_page` for the same `(era, page)` and confirm it returns `Error::<T>::AlreadyClaimed`, proving the reward is permanently unrecoverable.

### Citations

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L381-391)
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L491-513)
```rust
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
