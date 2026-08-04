## Analysis

The Vault report's core broken invariant is: **state is marked as "settled" before the token transfer that constitutes the settlement has actually succeeded**, so a failing transfer causes either a full revert (DoS) or, if failures are swallowed, silent unrecoverable fund loss for the affected leg while the rest of the accounting has already moved on.

The closest local analog in this repository is not `pallet-utility`'s `batch_all` (that rollback-on-failure behavior is documented, intentional, and safely revertible — no state is left inconsistent) or `pallet-message-queue` (which already processes each message inside its own `storage::with_transaction` and rolls back per-message on error, see `substrate/frame/message-queue/src/lib.rs:1569-1587`). Instead, it is the reward-provider payout path in `pallet-staking-async`, where the "claimed" marker is persisted **before** the actual currency transfer is attempted, and a transfer failure is silently swallowed instead of being propagated or retried.

### Title
Nominator/validator reward permanently lost when settlement transfer fails after payout state is marked claimed - (File: substrate/frame/staking-async/src/pallet/impls.rs)

### Summary
`do_payout_stakers_by_page` marks an era/page reward as claimed via `Eras::<T>::set_rewards_as_claimed` before the actual token settlement is performed. The settlement itself, in `make_payout_from_provider`, is a real `T::Currency::transfer` that can fail (e.g. `FundsUnavailable`, frozen/locked destination, existential-deposit violations under `Preservation::Expendable`). On failure, the code only logs the error and returns `None`; it never returns `Err` from the dispatchable, and the page/era claim marker is never reverted.

### Finding Description [1](#0-0) 
`Eras::<T>::set_rewards_as_claimed(era, &stash, page)` is executed unconditionally once the page's exposure is loaded, well before any transfer of funds occurs.

The reward is then distributed via `payout_from_provider`: [2](#0-1) 
which calls `make_payout_from_provider` for the validator and each nominator: [3](#0-2) 
If `T::Currency::transfer` returns an `Err` (destination account frozen by another pallet's lock/freeze, `Preservation::Expendable` failing to keep the account alive, provider-reference issues, etc.), the function logs the error and returns `None` — the caller (`payout_from_provider`) simply skips emitting a `Rewarded` event for that account and moves on to the next nominator, with the whole `do_payout_stakers_by_page` extrinsic still returning `Ok(...)`.

Because the claim marker was already persisted at line 386, there is no mechanism to retry or reclaim this specific reward: the era/page is permanently marked `is_rewards_claimed`, so calling `payout_stakers`/`payout_stakers_by_page` again for that era/page returns `Error::<T>::AlreadyClaimed`. The reward amount remains stuck in the era's staker-reward pot account indefinitely with no user-facing recovery path.

This violates the required invariant that "payout state must only advance after ... settlement succeed atomically" — here the payout state (claimed flag) advances unconditionally, decoupled from the success of the individual settlement transfers that are supposed to back it.

### Impact Explanation
`payout_stakers` / `payout_stakers_by_page` are public, unprivileged dispatchables callable by any signed account on behalf of any validator/era/page: [4](#0-3) 
Any account whose reward destination account is, or becomes, in a state that causes `Currency::transfer` to fail (frozen balance from an unrelated pallet, insufficient balance to satisfy ED under `Preservation::Expendable` for a brand-new destination, etc.) will have that specific reward silently and permanently lost the moment anyone (not necessarily the nominator) triggers the payout for that page. There is no retry, no error surfaced to the caller, and the funds are stranded in the reward pot — a "permanent user-fund lock" outcome achievable by any unprivileged caller, without needing a malicious validator, relayer, or governance actor.

### Likelihood Explanation
Likelihood is moderate to high: `Currency::transfer` failures are not exotic — any nominator/validator account subject to a freeze/lock from another pallet (e.g. democracy locks, staking locks stacking oddly, nomination-pools ED freezes) or one that has been fully drained to below ED can trigger `FundsUnavailable`/`Expendability` failures. Since payout calls are permissionless and can be invoked by anyone at any time for any page, an attacker can also proactively manipulate the target account's balance/lock state (where feasible) right before calling payout to force the failure and lock the reward.

### Recommendation
Do not mark the page/era as claimed until the settlement transfers have been attempted and their outcome is known. Either:
- Perform the transfers first, and only call `set_rewards_as_claimed` after successful (or intentionally accepted-partial) settlement, or
- On transfer failure, keep the specific nominator's share unclaimed / re-queue it for future retry, or route the failed amount to a recoverable location (e.g. a per-page "unclaimed remainder" storage item that can be swept via a dedicated dispatchable) instead of silently dropping it, or
- Return an error (or partial success info) from `do_payout_stakers_by_page` so the caller/relevant nominator is aware and can remediate their account (e.g. keep-alive / unfreeze) before retrying, without losing "already claimed" state for the whole page.

### Proof of Concept
1. A validator has at least one nominator `N` in an exposure page.
2. Cause `N`'s account to be in a state where an inbound `Currency::transfer` with `Preservation::Expendable` fails to reach `N` — e.g. `N`'s account has a lock/freeze from `pallet-nomination-pools` or another consumer that, combined with the reward's transfer semantics/provider-reference bookkeeping, causes `FundsUnavailable` or `NoProviders`/`FundsUnavailable` at the point of the transfer target update. (The exact trigger depends on lock provider/consumer configuration on the runtime but is reachable without any privileged action.)
3. Any account calls `Staking::payout_stakers_by_page(origin, validator_stash, era, page)`.
4. `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` executes (impls.rs:386), then `payout_from_provider` runs and `make_payout_from_provider` for `N` hits the `Err` branch (impls.rs:602-616), logs the error, and returns `None` — no `Rewarded` event for `N`, no error returned to the caller; extrinsic returns `Ok`.
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
