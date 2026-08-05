## Title
Reward pages are irrevocably marked "claimed" before the transfer that pays them succeeds, permanently burning stakers' rewards on transfer failure — ([File: substrate/frame/staking-async/src/pallet/impls.rs])

### Summary
The external report's core broken invariant is: code assumes a payment beneficiary can always receive the transferred funds, and performs the state-advancing/settlement action without verifying that the transfer actually landed — so a legitimate, unprivileged recipient can permanently lose funds when the receive path fails. The same pattern exists in `pallet-staking-async`'s reward-payout flow: the page-claimed marker is flipped to "claimed" *before* the actual currency transfer to the payee is attempted, and if that transfer fails the code merely logs a defensive warning and drops the reward — with no way to ever retry it.

### Finding Description
In `do_payout_stakers_by_page`, the era/page reward is marked as claimed up front: [1](#0-0) 

This happens before any transfer of value to the validator/nominators is attempted — the payout logic (`payout_from_provider` → `make_payout_from_provider`, or the validator-incentive transfer) runs afterward: [2](#0-1) 

`make_payout_from_provider` performs a real `Currency::transfer` (unlike the legacy mint-based path which creates the destination account regardless of ED). If the transfer fails — e.g., because `RewardDestination::Account(dest_account)` points to an account that does not exist and the reward amount is below the existential deposit, so the fungible implementation cannot create/credit it — the function only logs an error and returns `None`, silently dropping the reward: [3](#0-2) 

The same "log-and-drop" pattern exists for the validator incentive transfer: [4](#0-3) 

Because `Eras::<T>::set_rewards_as_claimed(era, &stash, page)` was already committed at line 386 before any of these transfers were attempted, there is no mechanism to retry or reclaim the reward once the transfer fails — the page can never be claimed again (`is_rewards_claimed` will return `true` for it going forward). This directly violates the required invariant that "payout state must only advance after ... settlement succeed[s] atomically," analogous to the external report's flaw where the code assumes the receiving side can always accept the payment and performs the state transition regardless.

### Impact Explanation
`payout_stakers`/`payout_stakers_by_page` is a public, permissionless extrinsic that anyone can call on behalf of a stash. Any nominator or validator whose `RewardDestination::Account` target is a fresh/non-existent account, or is otherwise unable to receive the transfer (e.g., insufficient ED, frozen/no-provider state), has their entire page reward permanently and irrecoverably lost the moment someone triggers the payout — the pot's funds are neither delivered to the beneficiary nor returned, and the claim is marked done forever. This is a direct fund-loss/permanent-lock condition under the "Balances ... must conserve value and settle exactly once to the rightful beneficiary" pivot.

### Likelihood Explanation
This requires no malicious actor, governance, or admin action — only an ordinary nominator/validator configuring `RewardDestination::Account` to point at an account that has never held a balance (a very common real-world configuration, e.g., a fresh cold-storage address), combined with a per-page reward share below the existential deposit (easily happens for validators with many small nominators). Anyone (not just the stash owner) can call `payout_stakers`, so the failure is trivially triggerable by an unrelated caller.

### Recommendation
Perform the transfer/settlement first and only call `Eras::<T>::set_rewards_as_claimed` after the transfer (or the full batch of transfers) succeeds, or make the claimed-marking transactional/rollback-safe so a failed transfer does not permanently forfeit the reward. Alternatively, retain the reward in the era pot and expose a distinct, retryable "unclaimed reward" state instead of unconditionally marking the page as claimed regardless of transfer outcome.

### Proof of Concept
1. Configure a nominator's `Payee` as `RewardDestination::Account(fresh_account)` where `fresh_account` has zero balance and no existential deposit.
2. Ensure the nominator's computed page reward share for an era is less than `ExistentialDeposit`.
3. Call `payout_stakers_by_page(validator_stash, era, page)` (permissionless).
4. Observe: `Eras::set_rewards_as_claimed` is set for `(era, stash, page)` at line 386; `make_payout_from_provider` for the nominator fails the `T::Currency::transfer` call (line 602-616), logs the error, and returns `None` — no funds reach `fresh_account`.
5. Call the same extrinsic (or `payout_stakers`) again for the same `(era, stash, page)` — it now returns `Error::AlreadyClaimed`, proving the reward is permanently and irrecoverably lost.

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
