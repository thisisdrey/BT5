## Analysis



### Title
Unbond/claim-payout in `pallet-nomination-pools` reverts entirely if the coupled reward transfer fails, permanently blocking a member's ability to exit their stake - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::unbond` (and `claim_payout`) unconditionally invoke `do_reward_payout`, which performs an external `T::Currency::transfer` from the pool's reward account to the member with `Preservation::Preserve`. That transfer call is propagated with `?`, so any failure of the reward-side transfer aborts the *entire* unbond transaction — including the unrelated principal-unbonding logic. This is the same coupling pattern as `OgvStaking`/`RewardsSource`: a core governance/exit function (`unstake`/`unbond`) is made to depend atomically on a secondary, more fragile reward-accounting call, so a revert in the reward path disables the primary function (unbonding/exiting).

### Finding Description
`Call::unbond` first fetches the member/pool state, then calls `reward_pool.update_records(...)` followed by `Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?` **before** performing the actual `T::StakeAdapter::unbond(...)` that reduces the member's bonded stake: [1](#0-0) 

`do_reward_payout` computes `pending_rewards` and, if non-zero, performs a live currency transfer from the reward account to the member using `Preservation::Preserve`, propagating any error with `?`: [2](#0-1) 

The same `do_reward_payout` call gate is used by `claim_payout`/`do_claim_payout`: [3](#0-2) 

The pallet's own `try_state` checks acknowledge that the reward account's reducible balance can fall short of the amount it is expected to be able to pay out, e.g. due to existential-deposit changes or residual rounding: it only logs a warning ("should only happen because ED has changed recently... pool operators should be notified to top up the reward account") rather than treating it as impossible: [4](#0-3) [5](#0-4) 

This mirrors the external report exactly: `RewardsSource.setRewardsTarget(0)` made `collectRewards` revert, which in turn made `OgvStaking.stake/unstake/extend` revert because those functions unconditionally call `_collectRewards` first. Here, if the reward-account transfer inside `do_reward_payout` fails for any reason (transient ED shortfall in the reward account, dust/rounding edge cases flagged by the pallet's own sanity checks, or any other `Currency::transfer` failure path), the `?` operator aborts the whole `unbond` extrinsic, so the member cannot unbond their principal stake at all until the underlying reward-account balance issue is resolved by a pool operator (`adjust_pool_deposit`) — a third party the member has no control over.

### Impact Explanation
A member's core exit primitive (`unbond`, and analogously `claim_payout`) can be completely and unconditionally blocked by a failure in a logically separate concern (reward payout), even though the member has sufficient bonded stake to legitimately unbond. This is a "permanent user-fund lock" class impact: the member cannot start the unbonding clock, so their funds remain staked and inaccessible until an unrelated actor (pool operator) intervenes to fix the reward account's balance — with no owner recourse in the pallet itself for the affected member.

### Likelihood Explanation
The pallet's own `do_try_state` diagnostics acknowledge that the reward account balance vs. required-payout invariant can be violated ("should only happen because ED has changed recently", and a residual dust-accumulation warning for `pending_rewards > leftover balance`), meaning the failure mode is not purely theoretical. Because `do_reward_payout` is invoked unconditionally on every `unbond`/`claim_payout` call before the stake-affecting logic runs, any transient shortfall in the reward account converts into a hard failure of core staking functionality for every member of the affected pool.

### Recommendation
Decouple reward payout from the core unbond/exit path: wrap the reward-transfer call in `do_reward_payout` so that a payout failure does not prevent `unbond` from proceeding with unbonding the member's principal stake (e.g., skip/queue the reward payout and emit an event on failure rather than propagating the error with `?`), similar to the try/catch remediation adopted for `OgvStaking`/`RewardsSource`.

### Proof of Concept
1. Create a nomination pool and have a member accrue pending rewards (`deposit_rewards`).
2. Engineer the reward account's reducible balance to fall short of `pending_rewards` at payout time (e.g., a runtime `ExistentialDeposit` increase without a corresponding `adjust_pool_deposit` top-up, as anticipated by the pallet's own `do_try_state` warning at `substrate/frame/nomination-pools/src/lib.rs:3953-3972`), so `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)` in `do_reward_payout` (`substrate/frame/nomination-pools/src/lib.rs:3556-3563`) returns an `Err`.
3. Call `Pools::unbond(origin, member_account, unbonding_points)`.
4. Observe that the whole extrinsic fails with the underlying `Currency::transfer` error, and the member's active stake remains fully bonded — they cannot unbond despite having valid bonded points, exactly as `OgvStaking.unstake` was blocked by a reverting `RewardsSource.collectRewards`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2275-2295)
```rust
			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3552-3563)
```rust
		// IFF the reward is non-zero alter the member and reward pool info.
		member.last_recorded_reward_counter = current_reward_counter;
		reward_pool.register_claimed_reward(pending_rewards);

		T::Currency::transfer(
			&bonded_pool.reward_account(),
			member_account,
			pending_rewards,
			// defensive: the depositor has put existential deposit into the pool and it stays
			// untouched, reward account shall not die.
			Preservation::Preserve,
		)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3753-3769)
```rust
	pub(crate) fn do_claim_payout(
		signer: T::AccountId,
		member_account: T::AccountId,
	) -> DispatchResult {
		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_claim_payout(),
				Error::<T>::DoesNotHavePermission
			);
		}
		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?;

		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);
		Ok(())
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3953-3972)
```rust
		for id in reward_pools {
			let account = Self::generate_reward_account(id);
			if T::Currency::reducible_balance(&account, Preservation::Expendable, Fortitude::Polite) <
				T::Currency::minimum_balance()
			{
				log!(
					warn,
					"reward pool of {:?}: {:?} (ed = {:?}), should only happen because ED has \
					changed recently. Pool operators should be notified to top up the reward \
					account",
					id,
					T::Currency::reducible_balance(
						&account,
						Preservation::Expendable,
						Fortitude::Polite
					),
					T::Currency::minimum_balance(),
				)
			}
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3998-4016)
```rust
		RewardPools::<T>::iter_keys().try_for_each(|id| -> Result<(), TryRuntimeError> {
			// the sum of the pending rewards must be less than the leftover balance. Since the
			// reward math rounds down, we might accumulate some dust here.
			let pending_rewards_lt_leftover_bal = RewardPool::<T>::current_balance(id) >=
				pools_members_pending_rewards.get(&id).copied().unwrap_or_default();

			// If this happens, this is most likely due to an old bug and not a recent code change.
			// We warn about this in try-runtime checks but do not panic.
			if !pending_rewards_lt_leftover_bal {
				log!(
					warn,
					"pool {:?}, sum pending rewards = {:?}, remaining balance = {:?}",
					id,
					pools_members_pending_rewards.get(&id),
					RewardPool::<T>::current_balance(id)
				);
			}
			Ok(())
		})?;
```
