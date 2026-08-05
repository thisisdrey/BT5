[1](#0-0) [2](#0-1) [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L65-79)
```rust
//! ## Rewards Algorithm
//!
//! The rewards algorithm is based on the Synthetix [StakingRewards.sol](https://web.archive.org/web/20251223190741/https://github.com/Synthetixio/synthetix/blob/develop/contracts/StakingRewards.sol)
//! smart contract.
//!
//! Rewards are calculated JIT (just-in-time), and all operations are O(1) making the approach
//! scalable to many pools and stakers.
//!
//! ### Resources
//!
//! - [This video series](https://www.youtube.com/watch?v=6ZO5aYg1GI8), which walks through the math
//!   of the algorithm.
//! - [This dev.to article](https://dev.to/heymarkkop/understanding-sushiswaps-masterchef-staking-rewards-1m6f),
//!   which explains the algorithm of the SushiSwap MasterChef staking. While not identical to the
//!   Synthetix approach, they are quite similar.
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L900-922)
```rust
	fn set_pool_reward_rate_per_block(
		admin: &T::AccountId,
		pool_id: PoolId,
		new_reward_rate_per_block: T::Balance,
	) -> DispatchResult {
		let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
		ensure!(pool_info.admin == *admin, BadOrigin);
		ensure!(
			new_reward_rate_per_block > pool_info.reward_rate_per_block,
			Error::<T>::RewardRateCut
		);

		// Always start by updating the pool rewards.
		let rewards_per_token = Self::reward_per_token(&pool_info)?;
		let mut pool_info = Self::update_pool_rewards(&pool_info, rewards_per_token)?;

		pool_info.reward_rate_per_block = new_reward_rate_per_block;
		Pools::<T>::insert(pool_id, pool_info);

		Self::deposit_event(Event::PoolRewardRateModified { pool_id, new_reward_rate_per_block });

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L526-527)
```rust
	/// The reward counter at the time of this member's last payout claim.
	pub last_recorded_reward_counter: T::RewardCounter,
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1402-1422)
```rust
	/// Update the recorded values of the reward pool.
	///
	/// This function MUST be called whenever the points in the bonded pool change, AND whenever the
	/// the pools commission is updated. The reason for the former is that a change in pool points
	/// will alter the share of the reward balance among pool members, and the reason for the latter
	/// is that a change in commission will alter the share of the reward balance among the pool.
	fn update_records(
		&mut self,
		id: PoolId,
		bonded_points: BalanceOf<T>,
		commission: Perbill,
	) -> Result<(), Error<T>> {
		let balance = Self::current_balance(id);

		let (current_reward_counter, new_pending_commission) =
			self.current_reward_counter(id, bonded_points, commission)?;

		// Store the reward counter at the time of this update. This is used in subsequent calls to
		// `current_reward_counter`, whereby newly pending rewards (in points) are added to this
		// value.
		self.last_recorded_reward_counter = current_reward_counter;
```
