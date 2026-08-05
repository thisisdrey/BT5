The claim accurately matches the code. The logic confirms:

1. `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` at line 386 is called unconditionally before any transfer occurs, and before the exposure/reward-points computation happens. [1](#0-0) 

2. `payout_from_provider` calls `make_payout_from_provider` for the validator and each nominator, and on success emits a `Rewarded` event but on `None` (failure) simply skips it — no error propagation. [2](#0-1) 

3. `make_payout_from_provider` performs the actual `T::Currency::transfer` from the staker-rewards pot to the payout account with `Preservation::Expendable`; on `Err`, it only logs the error and returns `None`, never propagating a failure to the caller. [3](#0-2) 

4. Since the claim marker was already set at line 386 before this transfer attempt, and there's no code path that reverts `set_rewards_as_claimed` on transfer failure, a subsequent call for the same `(era, stash, page)` will hit the `AlreadyClaimed` check at line 381-384 and fail permanently — the code path is exactly as described, with `do_payout_stakers_by_page` still returning `Ok(...)` overall since only the individual `Rewarded` event is skipped. [4](#0-3) 

5. The dispatchable `payout_stakers` is confirmed to be a public, unprivileged, signed extrinsic callable by anyone on behalf of any validator/era. [5](#0-4) 

This is a genuine unpermissioned-callable path leading to permanent, unrecoverable fund loss for the affected nominator/validator (funds stranded in the era's staker-reward pot with no retry or sweep mechanism), matching the "permanent user-fund lock" and "payout state must only advance after ... settlement succeed atomically" impact gate criteria. The claim's code citations, line numbers, and mechanics check out against the actual repository state.

Audit Report

## Title
Nominator/validator reward permanently lost when settlement transfer fails after payout state is marked claimed - (File: substrate/frame/staking-async/src/pallet/impls.rs)

## Summary
`do_payout_stakers_by_page` marks an era/page reward as claimed via `Eras::<T>::set_rewards_as_claimed` before the actual token settlement is performed. The settlement itself, in `make_payout_from_provider`, is a real `T::Currency::transfer` that can fail, and on failure the code only logs the error and returns `None` — it never returns `Err` from the dispatchable, and the page/era claim marker is never reverted.

## Finding Description
`Eras::<T>::set_rewards_as_claimed(era, &stash, page)` is executed unconditionally at impls.rs:386, before any transfer of funds occurs. The reward is then distributed via `payout_from_provider`, which calls `make_payout_from_provider` for the validator and each nominator. If `T::Currency::transfer` returns an `Err` (e.g. `FundsUnavailable`, frozen/locked destination, existential-deposit violations under `Preservation::Expendable`), `make_payout_from_provider` logs the error and returns `None` — the caller simply skips emitting a `Rewarded` event for that account and continues, with `do_payout_stakers_by_page` still returning `Ok(...)`. Because the claim marker was already persisted before the transfer attempt, there is no mechanism to retry or reclaim this specific reward: a subsequent call to `payout_stakers`/`payout_stakers_by_page` for that era/page returns `Error::<T>::AlreadyClaimed` at impls.rs:381-384. The reward amount remains stuck in the era's staker-reward pot account indefinitely with no recovery path. This violates the invariant that payout state must only advance after settlement succeeds atomically.

## Impact Explanation
`payout_stakers` / `payout_stakers_by_page` are public, unprivileged dispatchables callable by any signed account on behalf of any validator/era/page. Any account whose reward destination is, or becomes, in a state causing `Currency::transfer` to fail will have that specific reward silently and permanently lost the moment anyone triggers the payout for that page/era. There is no retry, no error surfaced to the caller, and funds are stranded in the reward pot — a permanent user-fund lock achievable by any unprivileged caller.

## Likelihood Explanation
Likelihood is moderate to high: `Currency::transfer` failures are not exotic — accounts subject to freezes/locks from other pallets, or accounts below existential deposit, can trigger `FundsUnavailable` or expendability failures. Since payout calls are permissionless and callable by anyone at any time for any page, the failure condition can be reached in normal operation or deliberately induced against a target account.

## Recommendation
Do not mark the page/era as claimed until settlement transfers have been attempted and their outcome is known. Perform transfers first and only call `set_rewards_as_claimed` after successful settlement, or on transfer failure keep the specific account's share unclaimed/re-queued for retry, or route failed amounts to a recoverable per-page "unclaimed remainder" storage item that can be swept via a dedicated dispatchable, and surface failure information to the caller instead of silently dropping it.

## Proof of Concept
1. A validator has at least one nominator `N` in an exposure page.
2. Cause `N`'s account to be in a state where an inbound `Currency::transfer` with `Preservation::Expendable` fails (e.g. lock/freeze from another pallet causing `FundsUnavailable`).
3. Any account calls `Staking::payout_stakers_by_page(origin, validator_stash, era, page)`.
4. `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` executes (impls.rs:386), then `make_payout_from_provider` for `N` hits the `Err` branch (impls.rs:602-616), logs the error, and returns `None` — no `Rewarded` event for `N`, no error returned to the caller; extrinsic returns `Ok`.
5. Any subsequent call to `payout_stakers_by_page` for the same `(era, stash, page)` immediately fails with `Error::<T>::AlreadyClaimed` (impls.rs:381-384), permanently preventing `N` from ever receiving that reward.

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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L393-403)
```rust
		// Input data seems good, no errors allowed after this point

		let era_reward_points = Eras::<T>::get_reward_points(era);
		let total_reward_points = era_reward_points.total;
		let validator_reward_points =
			era_reward_points.individual.get(&stash).copied().unwrap_or_else(Zero::zero);

		// Nothing to do if they have no reward points.
		if validator_reward_points.is_zero() {
			return Ok(Some(T::WeightInfo::payout_stakers_alive_staked(0)).into());
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L598-616)
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

**File:** substrate/frame/staking/src/pallet/mod.rs (L1729-1738)
```rust
		#[pallet::call_index(18)]
		#[pallet::weight(T::WeightInfo::payout_stakers_alive_staked(T::MaxExposurePageSize::get()))]
		pub fn payout_stakers(
			origin: OriginFor<T>,
			validator_stash: T::AccountId,
			era: EraIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;
			Self::do_payout_stakers(validator_stash, era)
		}
```
