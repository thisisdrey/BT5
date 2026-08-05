Audit Report

## Title
Reward payout marks era/page as claimed before the transfer succeeds, permanently losing staker rewards on transfer failure - (`substrate/frame/staking-async/src/pallet/impls.rs`)

## Summary
`do_payout_stakers_by_page` calls `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` at line 386 before any reward transfer is attempted, and the comment at line 393 explicitly states no errors are allowed after that point. However `make_payout_from_provider` and `transfer_validator_incentive` swallow `T::Currency::transfer` failures (log and return `None`/unit) instead of propagating a `DispatchError`, so the extrinsic still returns `Ok(...)` even when a payout transfer fails, leaving the claimed marker permanently set with no funds delivered.

## Finding Description
In `do_payout_stakers_by_page`, after validating the era/page and stash, the function unconditionally commits the claim marker: [1](#0-0) . This happens before the actual value transfer to nominators and the validator.

The subsequent per-nominator payout, when the DAP path is active, calls `Self::make_payout_from_provider`, which performs `T::Currency::transfer(&staker_rewards_pot, &payout_account, amount, Preservation::Expendable)` and, on failure, only logs the error and returns `None`: [2](#0-1) . The caller that iterates nominators simply skips emitting a `Rewarded` event when `None` is returned and continues to the next nominator: [3](#0-2) . The validator's own incentive path, `transfer_validator_incentive`, has the identical pattern — on transfer failure it only logs a warning, deposits an `Unexpected` event, and triggers a `defensive!` marker, but does not return an `Err`: [4](#0-3) .

Because none of these failure branches produce a `DispatchError` that bubbles up out of `do_payout_stakers_by_page`, the dispatchable completes with `Ok(...)`. FRAME's transactional dispatch wrapper only rolls back storage changes when the call returns `Err`; since it returns `Ok`, the `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` write made earlier in the very same call is never reverted. The subsequent `AlreadyClaimed` guard at lines 381-384 then permanently blocks any retry for that `(era, stash, page)` combination, since `is_rewards_claimed` will now return `true` forever.

## Impact Explanation
This causes a permanent, unrecoverable loss of a specific staker's (nominator's or validator's) reward for a given era/page: the claimed flag is set irreversibly while the actual `T::Currency::transfer` never lands. This falls under the accepted "duplicate settlement or payout" / "permanent user-fund lock" impact category, since payout state (`Eras::<T>::ClaimedRewards`) advances even though the transfer failed and never completed — the pallet is required to only advance payout state after settlement succeeds atomically, and it does not do so here. Because there is no way to re-trigger payout once `set_rewards_as_claimed` is set, affected funds sitting in the `staker_rewards_pot`/incentive pot for that era become effectively stuck relative to that beneficiary.

## Likelihood Explanation
`payout_stakers`/`payout_stakers_by_page` are unprivileged, permissionless dispatchables callable by any signed account for any validator/era/page. The `T::Currency::transfer(..., Preservation::Expendable)` call can realistically fail under ordinary on-chain conditions — e.g., a small dust reward destined to a not-yet-existing account failing the existential deposit requirement, or a destination account with holds/freezes that block receipt of additional funds — none of which require a malicious validator, governance action, or leaked key. This makes the bug reachable by normal usage rather than a contrived edge case.

## Recommendation
Restructure `do_payout_stakers_by_page` so that `Eras::<T>::set_rewards_as_claimed` is only committed after all payout transfers (nominators + validator incentive) have been attempted and confirmed successful, or have `make_payout_from_provider`/`transfer_validator_incentive` propagate transfer failures as a hard `Err` so the whole extrinsic — including the claim marker — is rolled back atomically. Alternatively, track failed individual payouts in a retry-able pending-payout queue rather than unconditionally marking the entire page as claimed regardless of per-beneficiary transfer outcome.

## Proof of Concept
1. Configure a nominator's `RewardDestination::Account(x)` where account `x` does not yet exist and the computed `nominator_reward` for the target era/page is below the existential deposit, so `T::Currency::transfer(&staker_rewards_pot, &x, amount, Preservation::Expendable)` returns an error (e.g. `TokenError::BelowMinimum`).
2. Call `payout_stakers(era, validator_stash, page)` (or `payout_stakers_by_page`) as any signed account — this is a public, unprivileged extrinsic.
3. Observe that `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` is committed at line 386 before the transfer attempt; the transfer inside `make_payout_from_provider` fails, is logged, and `None` is returned, so no `Rewarded` event is emitted for that nominator, while `do_payout_stakers_by_page` still returns `Ok(...)`.
4. Re-call `payout_stakers` for the same `(era, validator_stash, page)`: it immediately fails with `Error::<T>::AlreadyClaimed` at lines 381-384, proving the reward can never be retried or delivered even though the transfer never completed.

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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L503-513)
```rust
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L600-616)
```rust
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

**File:** substrate/frame/staking-async/src/pallet/impls.rs (L781-802)
```rust
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
