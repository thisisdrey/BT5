### Title
`NominationPools::api_member_total_balance` under-reports a pool member's true claimable value by excluding accrued but unclaimed rewards - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The nomination-pools runtime API `member_total_balance`, exposed via `pallet_nomination_pools_runtime_api::NominationPoolsApi::member_total_balance`, is implemented as `NominationPools::api_member_total_balance` which simply calls `PoolMember::total_balance()`. That helper sums only the member's active (bonded) balance and unbonding balance, but never adds the member's accrued, unclaimed pool rewards, even though a separate `pending_rewards()` calculation exists in the same pallet for exactly this purpose. This mirrors the Ammplify `ViewFacet.queryAssetBalances` bug: a "total value" view accessor omits a category of already-accrued-but-uncollected value (uniswap fees there, pool rewards here), causing the query to under-report the actual value a member is entitled to.

### Finding Description
`PoolMember::total_balance()` computes:
```
active_balance = pool.points_to_balance(self.active_points())
unbonding_balance = sum over unbonding_eras of era_pool.point_to_balance(points)
return active_balance + unbonding_balance
``` [1](#0-0) 

This value is exposed as the pool member's "total balance" via: [2](#0-1) 

However, the pallet separately tracks a distinct, real, already-existing value pool: unclaimed rewards, computed by `PoolMember::pending_rewards()` against the reward pool's `current_reward_counter`: [3](#0-2) [4](#0-3) 

These pending rewards sit in the pool's reward account (`RewardPool::current_balance`) and are unambiguously owed to the member — calling `claim_payout` transfers exactly this amount to the member: [5](#0-4) 

The runtime API `member_total_balance` is documented as "the total contribution of a pool member including any balance that is unbonding," and the pallet doc comment for `api_member_total_balance` states it "Includes balance that is unbonded from staking but not claimed yet from the pool": [6](#0-5) [7](#0-6) 

Neither the trait doc nor the implementation ever adds `pending_rewards()`. Consumers (wallets, exchanges, custodial/staking dashboards, or any on-chain/off-chain logic built on top of this runtime API to value a user's stake) will see a value strictly smaller than what the member can actually withdraw once they call `claim_payout` followed by `unbond`/`withdraw_unbonded`. This is structurally identical to the Ammplify bug: a value-reporting function omits an already-accrued-but-uncollected earnings component that a different code path (the actual claim/collect flow) does account for.

### Impact Explanation
Any caller of the `member_total_balance` runtime API (RPC clients, block explorers, custodians computing collateral/backing for wrapped or bridged representations of pooled stake, or any smart contract/off-chain service using this figure to make decisions) receives an understated balance. The magnitude of the discrepancy grows with how long a member goes without calling `claim_payout` — pool rewards accumulate continuously as the bonded stake earns staking rewards, so for pools/members with infrequent manual claims, the gap between reported and real value can be substantial and is not bounded to a negligible dust amount. This can lead to downstream mis-valuation, mis-pricing of any dependent instrument, or a user incorrectly concluding their pool position holds less value than it truly does.

### Likelihood Explanation
This triggers automatically and unconditionally on every call: no attacker action, special pool configuration, or malicious actor is required. Any pool member with a non-zero, non-just-claimed reward counter delta will see this discrepancy every single time `member_total_balance`/`api_member_total_balance` is queried, exactly like the referenced `queryAssetBalance` bug being present on every non-`MAKER_NC`-only call.

### Recommendation
Update `Pallet::<T>::api_member_total_balance` (and/or `PoolMember::total_balance()`) to add the member's `pending_rewards()` computed against the reward pool's `current_reward_counter`, mirroring the logic already used in `try_state` checks and `do_reward_payout`:
```rust
pub fn api_member_total_balance(who: T::AccountId) -> BalanceOf<T> {
    PoolMembers::<T>::get(who.clone())
        .map(|m| {
            let pending = RewardPools::<T>::get(m.pool_id)
                .zip(BondedPools::<T>::get(m.pool_id))
                .and_then(|(reward_pool, bonded_pool)| {
                    let commission = bonded_pool.commission.current();
                    reward_pool
                        .current_reward_counter(m.pool_id, bonded_pool.points, commission)
                        .ok()
                        .and_then(|(rc, _)| m.pending_rewards(rc).ok())
                })
                .unwrap_or_default();
            m.total_balance().saturating_add(pending)
        })
        .unwrap_or_default()
}
```

### Proof of Concept
1. Create a pool and add a member with some bonded points.
2. Call `deposit_rewards(X)` (or otherwise have staking rewards land in the reward account) so that the reward pool accrues rewards without the member calling `claim_payout`.
3. Query `NominationPools::api_pending_rewards(member)` — observe a non-zero value (e.g. as shown in `pending_rewards_per_member_works`): [8](#0-7) 
4. Simultaneously query `NominationPools::api_member_total_balance(member)` and note it equals only `active_balance + unbonding_balance`, omitting the pending rewards observed in step 3.
5. Call `claim_payout(member)` — the member actually receives the pending rewards amount, proving that value was real and owed, yet `member_total_balance` never reported it.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L533-559)
```rust
impl<T: Config> PoolMember<T> {
	/// The pending rewards of this member.
	fn pending_rewards(
		&self,
		current_reward_counter: T::RewardCounter,
	) -> Result<BalanceOf<T>, Error<T>> {
		// accuracy note: Reward counters are `FixedU128` with base of 10^18. This value is being
		// multiplied by a point. The worse case of a point is 10x the granularity of the balance
		// (10x is the common configuration of `MaxPointsToBalance`).
		//
		// Assuming roughly the current issuance of polkadot (12,047,781,394,999,601,455, which is
		// 1.2 * 10^9 * 10^10 = 1.2 * 10^19), the worse case point value is around 10^20.
		//
		// The final multiplication is:
		//
		// rc * 10^20 / 10^18 = rc * 100
		//
		// the implementation of `multiply_by_rational_with_rounding` shows that it will only fail
		// if the final division is not enough to fit in u128. In other words, if `rc * 100` is more
		// than u128::max. Given that RC is interpreted as reward per unit of point, and unit of
		// point is equal to balance (normally), and rewards are usually a proportion of the points
		// in the pool, the likelihood of rc reaching near u128::MAX is near impossible.

		(current_reward_counter.defensive_saturating_sub(self.last_recorded_reward_counter))
			.checked_mul_int(self.active_points())
			.ok_or(Error::<T>::OverflowRisk)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L573-606)
```rust
	/// Total balance of the member, both active and unbonding.
	/// Doesn't mutate state.
	///
	/// Worst case, iterates over [`Config::MaxUnbondingPools`] member unbonding pools to
	/// calculate member balance.
	pub fn total_balance(&self) -> BalanceOf<T> {
		let pool = match BondedPool::<T>::get(self.pool_id) {
			Some(pool) => pool,
			None => {
				// this internal function is always called with a valid pool id.
				defensive!("pool should exist; qed");
				return Zero::zero();
			},
		};

		let active_balance = pool.points_to_balance(self.active_points());

		let sub_pools = match SubPoolsStorage::<T>::get(self.pool_id) {
			Some(sub_pools) => sub_pools,
			None => return active_balance,
		};

		let unbonding_balance = self.unbonding_eras.iter().fold(
			BalanceOf::<T>::zero(),
			|accumulator, (era, unlocked_points)| {
				// if the `SubPools::with_era` has already been merged into the
				// `SubPools::no_era` use this pool instead.
				let era_pool = sub_pools.with_era.get(era).unwrap_or(&sub_pools.no_era);
				accumulator + (era_pool.point_to_balance(*unlocked_points))
			},
		);

		active_balance + unbonding_balance
	}
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L4195-4213)
```rust
impl<T: Config> Pallet<T> {
	/// Returns the pending rewards for the specified `who` account.
	///
	/// In the case of error, `None` is returned. Used by runtime API.
	pub fn api_pending_rewards(who: T::AccountId) -> Option<BalanceOf<T>> {
		if let Some(pool_member) = PoolMembers::<T>::get(who) {
			if let Some((reward_pool, bonded_pool)) = RewardPools::<T>::get(pool_member.pool_id)
				.zip(BondedPools::<T>::get(pool_member.pool_id))
			{
				let commission = bonded_pool.commission.current();
				let (current_reward_counter, _) = reward_pool
					.current_reward_counter(pool_member.pool_id, bonded_pool.points, commission)
					.ok()?;
				return pool_member.pending_rewards(current_reward_counter).ok();
			}
		}

		None
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L4311-4319)
```rust
	/// Contribution of the member in the pool.
	///
	/// Includes balance that is unbonded from staking but not claimed yet from the pool, therefore
	/// this balance can be higher than the staked funds.
	pub fn api_member_total_balance(who: T::AccountId) -> BalanceOf<T> {
		PoolMembers::<T>::get(who.clone())
			.map(|m| m.total_balance())
			.unwrap_or_default()
	}
```

**File:** substrate/frame/nomination-pools/runtime-api/src/lib.rs (L70-71)
```rust
		/// Returns the total contribution of a pool member including any balance that is unbonding.
		fn member_total_balance(who: AccountId) -> Balance;
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L1836-1856)
```rust
	#[test]
	fn pending_rewards_per_member_works() {
		ExtBuilder::default().build_and_execute(|| {
			let ed = Currency::minimum_balance();

			assert_eq!(Pools::api_pending_rewards(10), Some(0));
			deposit_rewards(30);
			assert_eq!(Pools::api_pending_rewards(10), Some(30));
			assert_eq!(Pools::api_pending_rewards(20), None);

			Currency::set_balance(&20, ed + 10);
			assert_ok!(Pools::join(RuntimeOrigin::signed(20), 10, 1));

			assert_eq!(Pools::api_pending_rewards(10), Some(30));
			assert_eq!(Pools::api_pending_rewards(20), Some(0));

			deposit_rewards(100);

			assert_eq!(Pools::api_pending_rewards(10), Some(30 + 50));
			assert_eq!(Pools::api_pending_rewards(20), Some(50));
			assert_eq!(Pools::api_pending_rewards(30), None);
```
