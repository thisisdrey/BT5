### Title
Reward-per-block timer keeps advancing during zero-stake windows, permanently skipping rewards owed for that period - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
The `pallet-asset-rewards` reward algorithm is an explicit Rust port of the Synthetix `StakingRewards.sol` model cited in the external report [1](#0-0) . Just like the reported bug, `reward_per_token` refuses to accrue when the pool has no stake, but the "last update" checkpoint is unconditionally advanced by every pool-touching call, so reward-rate-based emissions that occur while `total_tokens_staked == 0` are silently dropped instead of being deferred to the next staker.

### Finding Description
`reward_per_token` short-circuits to the stored value when nobody is staked, exactly mirroring the vulnerable Solidity pattern: [2](#0-1) 

`update_pool_rewards` is the function that persists the checkpoint, and it always writes `last_update_block = current_block_number()` regardless of whether `total_tokens_staked` was zero for the elapsed interval: [3](#0-2) 

This function is invoked from every public entry point that touches a pool — `stake`, `unstake`, `harvest_rewards`, and `set_pool_reward_rate_per_block` — via `update_pool_and_staker_rewards`/`update_pool_rewards`: [4](#0-3) [5](#0-4) 

Because `reward_rate_per_block` is a fixed per-block emission rate tied to the pool's `expiry_block` (not to actual stake presence), any block range during which `total_tokens_staked == 0` — e.g. between `create_pool` and the first `stake`, or after every staker fully `unstake`s until someone stakes again — has its notional reward simply discarded: `reward_per_token` returns the old stored value (correctly, to avoid a division by zero), but `update_pool_rewards` still moves `last_update_block` forward past that window. The reward budget that "should" have accrued during the zero-stake interval, computed against `last_block_reward_applicable(expiry_block)`, is never captured by any staker because the next computation starts counting from the new `last_update_block`, not from when stake was last non-zero.

Unlike Synthetix/Blueberry where the reward tokens simply sit unclaimed until admin intervention, here the pool has a hard `expiry_block` after which `last_block_reward_applicable` caps at expiry — so the skipped blocks are not just deferred, they permanently shrink the total number of "rewardable blocks" available for the whole pool lifetime. No code path re-extends the reward window to compensate for zero-stake gaps.

### Impact Explanation
This degrades intended reward-payout behavior for any `pallet-asset-rewards` deployment: pool admins fund pools expecting `reward_rate_per_block * (expiry_block - creation_block)` tokens to be distributed to stakers over the pool's life, but any period without stake (which any unprivileged staker can trigger by fully unstaking, or which naturally exists before the first stake) causes that proportional share of rewards to never be credited to anyone. This falls under "runtime bugs that compromise intended behavior" for reward/payout accounting, matching the required impact class.

### Likelihood Explanation
No privileged actor is required. An ordinary staker calling the permissionless `unstake` extrinsic to fully withdraw (bringing `total_tokens_staked` to zero) and later `stake` again reproduces the exact zero-stake gap; the very first staker after `create_pool` also always experiences this gap between pool creation and their `stake` call. This is a deterministic, always-reachable code path, not a race condition or admin-only scenario.

### Recommendation
Do not advance `last_update_block` past the point where `total_tokens_staked` became zero. Track the block at which the pool last had non-zero stake and cap `last_update_block` updates accordingly (or refuse to advance `last_update_block` at all when `total_tokens_staked.is_zero()`), consistent with the remediation applied upstream in Blueberry's PR #29.

### Proof of Concept
1. `create_pool` with `reward_rate_per_block = R` and `expiry_block = E`, at block `B0`. `total_tokens_staked = 0`, `last_update_block = 0`.
2. No one stakes until block `B1` (`B1 > B0`). Alice calls `stake` at `B1`: `update_pool_and_staker_rewards` computes `reward_per_token` — since `total_tokens_staked == 0`, it returns the stored (zero) value [6](#0-5)  — then `update_pool_rewards` sets `last_update_block = B1` [3](#0-2) .
3. From this point, rewards accrue correctly from `B1` to `E`, but the reward budget nominally allocated for blocks `[0, B1)` (or `[creation, B1)`) is never distributed to Alice or any future staker — it is simply skipped because `last_update_block` jumped straight to `B1`.
4. Repeat with a full `unstake` bringing `total_tokens_staked` back to zero at block `B2`, followed by a new `stake` at `B3`: the interval `[B2, B3)` is likewise skipped, permanently reducing the effective reward-earning window within the fixed `expiry_block` bound.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L472-502)
```rust
		#[pallet::call_index(1)]
		pub fn stake(origin: OriginFor<T>, pool_id: PoolId, amount: T::Balance) -> DispatchResult {
			let staker = ensure_signed(origin)?;

			// Always start by updating staker and pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;

			T::AssetsFreezer::increase_frozen(
				pool_info.staked_asset_id.clone(),
				&FreezeReason::Staked.into(),
				&staker,
				amount,
			)?;

			// Update Pools.
			pool_info.total_tokens_staked.ensure_add_assign(amount)?;

			Pools::<T>::insert(pool_id, pool_info);

			// Update PoolStakers.
			staker_info.amount.ensure_add_assign(amount)?;
			PoolStakers::<T>::insert(pool_id, &staker, staker_info);

			// Emit event.
			Self::deposit_event(Event::Staked { staker, pool_id, amount });

			Ok(())
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L775-784)
```rust
		pub fn update_pool_rewards(
			pool_info: &PoolInfoFor<T>,
			reward_per_token: T::Balance,
		) -> Result<PoolInfoFor<T>, DispatchError> {
			let mut new_pool_info = pool_info.clone();
			new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number();
			new_pool_info.reward_per_token_stored = reward_per_token;

			Ok(new_pool_info)
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L787-810)
```rust
		pub(super) fn reward_per_token(
			pool_info: &PoolInfoFor<T>,
		) -> Result<T::Balance, DispatchError> {
			if pool_info.total_tokens_staked.is_zero() {
				return Ok(pool_info.reward_per_token_stored);
			}

			let rewardable_blocks_elapsed: u32 =
				match Self::last_block_reward_applicable(pool_info.expiry_block)
					.ensure_sub(pool_info.last_update_block)?
					.try_into()
				{
					Ok(b) => b,
					Err(_) => return Err(Error::<T>::BlockNumberConversionError.into()),
				};

			Ok(pool_info.reward_per_token_stored.ensure_add(
				pool_info
					.reward_rate_per_block
					.ensure_mul(rewardable_blocks_elapsed.into())?
					.ensure_mul(PRECISION_SCALING_FACTOR.into())?
					.ensure_div(pool_info.total_tokens_staked)?,
			)?)
		}
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
