Based on my research, the strongest local analog is in `pallet-nomination-pools`, where `RewardPool::current_reward_counter` and `update_records` compute reward shares as `new_pending_rewards / bonded_points`, and this denominator (`bonded_pool.points`) can legitimately drop to zero (e.g. a pool that gets fully slashed, or a pool member set that fully unbonds) while its reward account still holds a leftover, un-attributed balance — mirroring the WrappedVault flaw of accruing/holding rewards against a base whose "supply" can be zero, with no owner/admin recovery path.

### Title
Reward-pool balance becomes permanently unclaimable when a pool's bonded points hit zero - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
`pallet-nomination-pools` tracks a per-pool reward counter (`RewardPool::current_reward_counter`, `update_records`) that divides pending rewards by `bonded_points` [1](#0-0) . When a pool's `bonded_pool.points` becomes zero (e.g. after a full slash wipes out all points, or every member unbonds), the pallet's own `try_state` code documents this as the expected "no more rewards" case ("this pool has been heavily slashed and cannot have any rewards anymore") [2](#0-1) . Analogous to the WrappedVault bug — where rewards kept accruing/settling against a supply that could be zero, with funds getting stuck at the zero address — any balance sitting in the pool's reward account at that point (already-deposited but not-yet-recorded rewards, or dust from a stopped payout stream) can no longer be attributed to any recorded reward counter, and there is no admin/root "reclaim" extrinsic analogous to `ownerClaim` to recover it.

### Finding Description
`do_reward_payout` requires `!member.active_points().is_zero()` for the claimant [3](#0-2) , and pulls the payable amount from `reward_pool.current_reward_counter(bonded_pool.id, bonded_pool.points, commission)`, which itself divides by `bonded_points` [4](#0-3) . If `bonded_pool.points` is zero (all members have unbonded/been slashed to zero points), `checked_from_rational(new_pending_rewards, bonded_points)` returns `None`, and the whole update fails (`Error::<T>::OverflowRisk`), so `update_records`/`current_reward_counter` can't even run to attribute the balance to a counter. The `do_try_state` sanity check explicitly special-cases and tolerates this ("else this pool has been heavily slashed and cannot have any rewards anymore") rather than treating it as an error [2](#0-1) .

Unlike `pallet-asset-rewards`, which provides `cleanup_pool` allowing the pool admin to reclaim the entire remaining reward-asset balance once there are no stakers left [5](#0-4) , nomination-pools has no equivalent "root/admin claim leftover reward-account balance" call. Once `bonded_pool.points` is zero, any balance above ED sitting in the pool's `reward_account()` is permanently orphaned: no extrinsic path can move it out, because every payout path (`do_reward_payout`, `do_claim_commission`) requires computing `current_reward_counter` against `bonded_pool.points`, which is unusable at zero.

### Impact Explanation
This matches the "permanent user-fund or bridge-state lock" category in the impact gate: value legitimately deposited into a pallet-controlled account becomes unrecoverable by any party (not just the depositor, but also governance/root, since no extrinsic exists to sweep it), differing from the fixed WrappedVault behavior which at least allows the owner to claim rewards sent to the zero-supply period.

### Likelihood Explanation
Reaching `bonded_pool.points == 0` while the reward account still holds an unattributed balance requires a full slash event or full unbonding of a pool member set, both of which are realistic, unprivileged-adjacent lifecycle events (slashing is driven by validator misbehavior, not by a malicious insider of this pallet) rather than governance/admin abuse — it is a structural gap rather than a rare edge condition, though it is not triggerable purely at will by an unprivileged attacker in one transaction, making likelihood moderate.

### Recommendation
Add a permissionless-or-permissioned sweep call (mirroring `asset-rewards::cleanup_pool`'s `ownerClaim`-style recovery) that lets the pool's root/depositor reclaim the reward-account balance above ED once `bonded_pool.points` is zero and there are no `PoolMembers` left for that pool, so the pool can be safely destroyed and its dust returned rather than being permanently locked.

### Proof of Concept
1. Create a nomination pool, join with the sole depositor.
2. Deposit rewards into the pool's reward account (`deposit_rewards` helper mirrors runtime staking payouts).
3. Slash the pool's bonded stash such that `bonded_pool.points` becomes `0` (full slash wipes points to zero, as covered by the `try_state` special case at [2](#0-1) ).
4. Attempt `claim_payout`/`do_claim_commission` — both fail (`OverflowRisk` or no computable counter) because `bonded_points` is zero.
5. Observe that the remaining balance in `generate_reward_account(id)` (above ED) can never be moved out by any subsequent extrinsic, since no cleanup/sweep function exists for nomination-pools reward accounts.

**Note on confidence**: I was not able to fully trace every runtime-level slashing path (`staking-async` slashing interactions with `T::StakeAdapter`) that drives `bonded_pool.points` to exactly zero within the index available to me, so the exact slashing trigger sequence should be verified against `substrate/frame/nomination-pools/src/lib.rs` and the staking pallet's slashing interface in a full checkout before treating this as fully confirmed.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1450-1512)
```rust
	fn current_reward_counter(
		&self,
		id: PoolId,
		bonded_points: BalanceOf<T>,
		commission: Perbill,
	) -> Result<(T::RewardCounter, BalanceOf<T>), Error<T>> {
		let balance = Self::current_balance(id);

		// Calculate the current payout balance. The first 3 values of this calculation added
		// together represent what the balance would be if no payouts were made. The
		// `last_recorded_total_payouts` is then subtracted from this value to cancel out previously
		// recorded payouts, leaving only the remaining payouts that have not been claimed.
		let current_payout_balance = balance
			.saturating_add(self.total_rewards_claimed)
			.saturating_add(self.total_commission_claimed)
			.saturating_sub(self.last_recorded_total_payouts);

		// Split the `current_payout_balance` into claimable rewards and claimable commission
		// according to the current commission rate.
		let new_pending_commission = commission * current_payout_balance;
		let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);

		// * accuracy notes regarding the multiplication in `checked_from_rational`:
		// `current_payout_balance` is a subset of the total_issuance at the very worse.
		// `bonded_points` are similarly, in a non-slashed pool, have the same granularity as
		// balance, and are thus below within the range of total_issuance. In the worse case
		// scenario, for `saturating_from_rational`, we have:
		//
		// dot_total_issuance * 10^18 / `minJoinBond`
		//
		// assuming `MinJoinBond == ED`
		//
		// dot_total_issuance * 10^18 / 10^10 = dot_total_issuance * 10^8
		//
		// which, with the current numbers, is a miniscule fraction of the u128 capacity.
		//
		// Thus, adding two values of type reward counter should be safe for ages in a chain like
		// Polkadot. The important note here is that `reward_pool.last_recorded_reward_counter` only
		// ever accumulates, but its semantics imply that it is less than total_issuance, when
		// represented as `FixedU128`, which means it is less than `total_issuance * 10^18`.
		//
		// * accuracy notes regarding `checked_from_rational` collapsing to zero, meaning that no
		//   reward can be claimed:
		//
		// largest `bonded_points`, such that the reward counter is non-zero, with `FixedU128` will
		// be when the payout is being computed. This essentially means `payout/bonded_points` needs
		// to be more than 1/1^18. Thus, assuming that `bonded_points` will always be less than `10
		// * dot_total_issuance`, if the reward_counter is the smallest possible value, the value of
		//   the
		// reward being calculated is:
		//
		// x / 10^20 = 1/ 10^18
		//
		// x = 100
		//
		// which is basically 10^-8 DOTs. See `smallest_claimable_reward` for an example of this.
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;

		Ok((current_reward_counter, new_pending_commission))
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3536-3537)
```rust
		// a member who has no skin in the game anymore cannot claim any rewards.
		ensure!(!member.active_points().is_zero(), Error::<T>::FullyUnbonding);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3985-3993)
```rust
			if !bonded_pool.points.is_zero() {
				let commission = bonded_pool.commission.current();
				let (current_rc, _) = reward_pool
					.current_reward_counter(d.pool_id, bonded_pool.points, commission)
					.unwrap();
				let pending_rewards = d.pending_rewards(current_rc).unwrap();
				*pools_members_pending_rewards.entry(d.pool_id).or_default() += pending_rewards;
			} // else this pool has been heavily slashed and cannot have any rewards anymore.
			total_balance_members += d.total_balance();
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L690-729)
```rust
		/// Cleanup a pool.
		///
		/// Origin must be the pool admin.
		///
		/// Cleanup storage, release any associated storage cost and return the remaining reward
		/// tokens to the admin.
		#[pallet::call_index(8)]
		pub fn cleanup_pool(origin: OriginFor<T>, pool_id: PoolId) -> DispatchResult {
			let who = ensure_signed(origin)?;

			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			ensure!(pool_info.admin == who, BadOrigin);

			let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
			ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);

			let pool_balance = T::Assets::reducible_balance(
				pool_info.reward_asset_id.clone(),
				&pool_info.account,
				Preservation::Expendable,
				Fortitude::Polite,
			);
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&pool_info.account,
				&pool_info.admin,
				pool_balance,
				Preservation::Expendable,
			)?;

			if let Some((who, cost)) = PoolCost::<T>::take(pool_id) {
				T::Consideration::drop(cost, &who)?;
			}

			Pools::<T>::remove(pool_id);

			Self::deposit_event(Event::PoolCleanedUp { pool_id });

			Ok(())
		}
```
