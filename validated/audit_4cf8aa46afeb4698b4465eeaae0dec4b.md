Audit Report

## Title
`reward_per_token` integer division with an under-scaled precision constant permanently zeroes reward accrual for all pool stakers - (File: substrate/frame/asset-rewards/src/lib.rs)

## Summary
`reward_per_token` in `substrate/frame/asset-rewards/src/lib.rs` scales the reward numerator by the fixed constant `PRECISION_SCALING_FACTOR = 4096` [1](#0-0) , then divides by `total_tokens_staked` via `ensure_div`, which performs plain integer division with truncation toward zero [2](#0-1) . Since `total_tokens_staked` is fully controlled by any unprivileged caller of the public `stake` extrinsic [3](#0-2) , once `reward_rate_per_block * rewardable_blocks_elapsed * 4096 < total_tokens_staked`, the reward-per-token delta truncates to `0`, permanently and silently halting reward accrual for that block window for every staker in the pool.

## Finding Description
The reward computation is:
```
reward_per_token_stored += reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
``` [4](#0-3) 

`PRECISION_SCALING_FACTOR` is a `u16` constant fixed at `4096` [1](#0-0) , far smaller than the WAD-style `10^18` precision used by comparable reward accounting elsewhere in the codebase (e.g., `nomination-pools`' `RewardCounter: FixedU128`). The pallet's own docs acknowledge the algorithm is modeled on Synthetix's `StakingRewards.sol` [5](#0-4) , which uses `1e18` precision — a roughly 14-order-of-magnitude difference from `4096`.

`total_tokens_staked` is incremented unconditionally by the public `stake` extrinsic with no ceiling relative to `reward_rate_per_block` [3](#0-2) . `reward_rate_per_block` can only be *increased*, never decreased, by the pool admin once stakers exist [6](#0-5) , so there is no admin-side mitigation once the pool has been griefed by a large stake. `derive_rewards` then divides the staker's share of the (possibly zeroed) delta by the same `PRECISION_SCALING_FACTOR` [7](#0-6) , so once `reward_per_token_stored` fails to advance in a window, that window's rewards are permanently lost for every staker in the pool — the JIT computation model never retroactively recomputes historical windows.

## Impact Explanation
This is a runtime bug that compromises intended pallet behavior: legitimate stakers who deposited real assets into a `RewardsPool` can have their configured, expected reward stream permanently and silently halted by any other unprivileged account, with no recovery path for the already-lost accrual window. This matches the "runtime bugs that compromise intended behavior" impact category for `pallet-asset-rewards`, a live pallet reachable from any runtime including it.

## Likelihood Explanation
The precision gap (4096 vs typical 10-18 decimal asset scales) is large enough to be triggered under ordinary, non-adversarial usage as a pool grows successful, and can be deliberately and cheaply triggered by any account holding enough of the staked asset to call the public, unprivileged `stake` extrinsic — no special capability, governance, or validator power is required.

## Recommendation
Replace the fixed `u16` `PRECISION_SCALING_FACTOR` with a full fixed-point representation (e.g., `FixedU128`/WAD-style `10^18` scaling matching `nomination-pools`' `RewardCounter`), or use rounding-aware helpers (e.g., `multiply_by_rational_with_rounding`) so that reward-per-token deltas do not truncate to zero as `total_tokens_staked` grows relative to `reward_rate_per_block`.

## Proof of Concept
1. Admin creates a pool with a modest `reward_rate_per_block` (e.g., `1_000` in a 12-decimal asset).
2. Staker A stakes a normal amount (e.g., `1_000_000_000_000`).
3. Any unprivileged attacker calls `stake` with a very large amount (e.g., `10_000_000_000_000_000`), which the pallet permits unconditionally [3](#0-2) .
4. Since `1_000 * 4096 = 4_096_000 << total_tokens_staked`, `reward_per_token` in lines 786-810 returns the unchanged `reward_per_token_stored` for every subsequent block [2](#0-1) .
5. Any subsequent `stake`, `unstake`, `harvest_rewards`, or `claim` call confirms Staker A accrues zero additional rewards despite the pool continuing to emit its configured `reward_rate_per_block`.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L65-68)
```rust
//! ## Rewards Algorithm
//!
//! The rewards algorithm is based on the Synthetix [StakingRewards.sol](https://web.archive.org/web/20251223190741/https://github.com/Synthetixio/synthetix/blob/develop/contracts/StakingRewards.sol)
//! smart contract.
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L472-490)
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
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L786-810)
```rust
		/// Derives the current reward per token for this pool.
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L812-824)
```rust
		/// Derives the amount of rewards earned by a staker.
		///
		/// This is a helper function for `update_pool_rewards` and should not be called directly.
		fn derive_rewards(
			staker_info: &PoolStakerInfo<T::Balance>,
			reward_per_token: &T::Balance,
		) -> Result<T::Balance, DispatchError> {
			Ok(staker_info
				.amount
				.ensure_mul(reward_per_token.ensure_sub(staker_info.reward_per_token_paid)?)?
				.ensure_div(PRECISION_SCALING_FACTOR.into())?
				.ensure_add(staker_info.rewards)?)
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
