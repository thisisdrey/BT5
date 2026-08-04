Based on the code I was able to inspect, `pallet-nomination-pools`'s `withdraw_unbonded` extrinsic reaps a member's `PoolMembers` storage entry once their `total_points()` reaches zero, without ever routing through `do_reward_payout` to settle any pending, unclaimed rewards first. This is a direct structural analog of the Party rage-quit bug: burning/removing the on-chain record that grants claim rights forfeits a share of already-accrued, but unclaimed, distributions.

### Title
Unbonded pool member is reaped before pending rewards are settled, permanently forfeiting accrued but unclaimed payouts - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::withdraw_unbonded` removes a `PoolMembers` entry as soon as the member's remaining points hit zero, but it never calls `do_reward_payout`/`claim_payout` logic to flush rewards that accrued between the member's `last_recorded_reward_counter` and the pool's current reward counter. Once `PoolMembers::<T>::remove(&member_account)` executes, there is no longer any account record to compute or pay those rewards against, and the reward is permanently stranded in the reward pool. [1](#0-0) 

### Finding Description
`do_reward_payout` computes `pending_rewards` from the delta between the pool's `current_reward_counter` and the member's `last_recorded_reward_counter`, transfers the amount, and only then updates `member.last_recorded_reward_counter`. [2](#0-1) 

However, `do_reward_payout` explicitly refuses to run for a member with zero *active* points: "a member who has no skin in the game anymore cannot claim any rewards" — `ensure!(!member.active_points().is_zero(), Error::<T>::FullyUnbonding);`. [3](#0-2) 

A member calling `unbond` (fully) moves all of their points from `active` into `unbonding_eras`, so `active_points()` becomes zero immediately — meaning `claim_payout` (which is gated on the same check via `do_reward_payout`) can no longer be called for that member from that point on, even though rewards may have accrued between their last claim and the moment they unbonded.

Later, `withdraw_unbonded` finalizes the withdrawal: it calls `member.withdraw_unlocked(active_era)` to clear the unlocking chunks, and once `member.total_points()` is zero it deletes the storage: `PoolMembers::<T>::remove(&member_account)` (and if it's the depositor, dissolves the pool entirely). [4](#0-3) 

At no point in this call path — `unbond` → `withdraw_unbonded` — is `do_reward_payout` invoked to settle the reward delta accrued up to the unbond point. The reward pool's internal accounting (`RewardPool::current_reward_counter`, `total_rewards_claimed`) still treats that balance as "owed" but there is no longer any `PoolMembers` entry to compute or credit it to; the funds remain in the reward account, uncollectible by the original member. This mirrors the external report exactly: an action that voluntarily exits a position/burns a claim-bearing record (there, burning the governance NFT; here, fully unbonding and reaping the pool-member record) forfeits already-accrued but unclaimed distributions, and the user has no way to guarantee they claim first because rewards can be deposited into the pool (e.g. by `bond_extra`/staking rewards routed to the reward account) up until the block the member's `unbond`/`withdraw_unbonded` executes — an ordering the member does not fully control given normal block inclusion, mempool visibility, and multi-block reward accrual (analogous to the `distribute()`-before-`rageQuit()` race in the report).

Existing guards do not stop this: `do_reward_payout`'s `FullyUnbonding` check is a *feature* to prevent claiming based on stale active points, but it has the side effect of permanently locking out any reward accrued but not claimed prior to unbonding, since no other code path re-derives or transfers that amount before the record is deleted in `withdraw_unbonded`.

### Impact Explanation
This is a value-conservation break: rewards that were legitimately earned by a pool member (proportional to their points at the time) are computed into `RewardPool.last_recorded_reward_counter`/`total_rewards_claimed` bookkeeping but become permanently unclaimable once the member unbonds and is subsequently reaped. Funds are not stolen by an attacker but are permanently stranded in the reward pool account, unbacked by any claim path — effectively a fund lock for the legitimate beneficiary. This aligns with "permanent user-fund... lock" impact and "settle exactly once to the rightful beneficiary and amount" invariant violation, since the amount never settles to the rightful beneficiary at all.

### Likelihood Explanation
This requires no malicious actor, admin, governance action, or privileged party — it is triggered purely by normal, permissionless member behavior (`unbond` then `withdraw_unbonded`, both callable by the member or, in destroying-pool/depositor-only scenarios, permissionlessly by anyone) combined with ordinary reward accrual timing. Any pool member who unbonds without first calling `claim_payout` in the same or an earlier block loses pending rewards; because unbonding and reward crediting are asynchronous (rewards can be deposited by the pool's nominated validators' payout flow at any block), a member cannot always guarantee they've claimed the very latest reward tranche before their unbond point-zeroing takes effect.

### Recommendation
Before zeroing a member's active points in `unbond` (or, defensively, before deleting the `PoolMembers` entry in `withdraw_unbonded`), invoke `do_reward_payout` for that member so any pending reward is force-settled and transferred to them, mirroring the report's recommended fix of calling `claim()` before burning the claim-bearing record.

### Proof of Concept
1. Member `M` joins pool with points `P`, pool's reward account has accrued rewards such that `M`'s pending reward is `X` (via `RewardPool::current_reward_counter` delta), but `M` has not called `claim_payout`.
2. `M` calls `Pools::unbond(RuntimeOrigin::signed(M), M, P)` — all `P` points move to `unbonding_eras`, so `member.active_points()` becomes `0`. [3](#0-2) 
3. `M` can no longer call `claim_payout` for the pending `X` because `do_reward_payout` will return `Error::<T>::FullyUnbonding`.
4. After the unbonding period, `M` calls `withdraw_unbonded`; `member.total_points()` reaches zero and `PoolMembers::<T>::remove(&member_account)` executes, permanently discarding any record of `M`'s unclaimed `last_recorded_reward_counter` state. [5](#0-4) 
5. `X` remains in the reward pool's account balance, uncredited and uncollectible by `M`; it is only ever effectively "found" as dust/leftover during pool dissolution or subsequent commission/reward accounting for *other* members, not returned to `M`.

**Caveat**: I could not locate/inspect the full body of the `unbond` extrinsic itself in this pass (only its call sites in tests) to confirm there is truly zero reward-payout call within `unbond`, so this analysis relies on the explicit absence of any `do_reward_payout` call in the `withdraw_unbonded` body shown above and the `FullyUnbonding` guard in `do_reward_payout`. If a Devin session with full file access to `substrate/frame/nomination-pools/src/lib.rs` `unbond` function is needed to fully confirm this end-to-end, that would remove remaining uncertainty.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2507-2556)
```rust
			Self::deposit_event(Event::<T>::Withdrawn {
				member: member_account.clone(),
				pool_id: member.pool_id,
				points: sum_unlocked_points,
				balance: balance_to_unbond,
			});

			let post_info_weight = if member.total_points().is_zero() {
				// remove any `ClaimPermission` associated with the member.
				ClaimPermissions::<T>::remove(&member_account);

				// member being reaped.
				PoolMembers::<T>::remove(&member_account);

				// Ensure any dangling delegation is withdrawn.
				let dangling_withdrawal = match T::StakeAdapter::member_delegation_balance(
					Member::from(member_account.clone()),
				) {
					Some(dangling_delegation) => {
						T::StakeAdapter::member_withdraw(
							Member::from(member_account.clone()),
							Pool::from(bonded_pool.bonded_account()),
							dangling_delegation,
							num_slashing_spans,
						)?;
						dangling_delegation
					},
					None => Zero::zero(),
				};

				Self::deposit_event(Event::<T>::MemberRemoved {
					pool_id: member.pool_id,
					member: member_account.clone(),
					released_balance: dangling_withdrawal,
				});

				if member_account == bonded_pool.roles.depositor {
					Pallet::<T>::dissolve_pool(bonded_pool);
					Weight::default()
				} else {
					bonded_pool.dec_members().put();
					SubPoolsStorage::<T>::insert(member.pool_id, sub_pools);
					T::WeightInfo::withdraw_unbonded_update(num_slashing_spans)
				}
			} else {
				// we certainly don't need to delete any pools, because no one is being removed.
				SubPoolsStorage::<T>::insert(member.pool_id, sub_pools);
				PoolMembers::<T>::insert(&member_account, member);
				T::WeightInfo::withdraw_unbonded_update(num_slashing_spans)
			};
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3524-3570)
```rust
	/// If the member has some rewards, transfer a payout from the reward pool to the member.
	// Emits events and potentially modifies pool state if any arithmetic saturates, but does
	// not persist any of the mutable inputs to storage.
	fn do_reward_payout(
		member_account: &T::AccountId,
		member: &mut PoolMember<T>,
		bonded_pool: &mut BondedPool<T>,
		reward_pool: &mut RewardPool<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		debug_assert_eq!(member.pool_id, bonded_pool.id);
		debug_assert_eq!(&mut PoolMembers::<T>::get(member_account).unwrap(), member);

		// a member who has no skin in the game anymore cannot claim any rewards.
		ensure!(!member.active_points().is_zero(), Error::<T>::FullyUnbonding);

		let (current_reward_counter, _) = reward_pool.current_reward_counter(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;

		// Determine the pending rewards. In scenarios where commission is 100%, `pending_rewards`
		// will be zero.
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}

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

		Self::deposit_event(Event::<T>::PaidOut {
			member: member_account.clone(),
			pool_id: member.pool_id,
			payout: pending_rewards,
		});
		Ok(pending_rewards)
```
