Audit Report

## Title
Public `stake()` extrinsic can permanently truncate/zero all future reward accrual in `pallet-asset-rewards` due to undersized `PRECISION_SCALING_FACTOR` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`pallet-asset-rewards::reward_per_token()` scales `reward_rate_per_block * blocks_elapsed` by a fixed `PRECISION_SCALING_FACTOR` of `4096` before dividing by `total_tokens_staked`, using floor integer division via `ensure_mul`/`ensure_div`. [1](#0-0)  Because `stake()` is a fully public, unprivileged extrinsic that lets any signed account add arbitrary amounts to `total_tokens_staked` with no upper bound relative to `reward_rate_per_block`, any staker can push the pool's stake total past the point where `reward_rate_per_block * blocks_elapsed * 4096 / total_tokens_staked` floors to `0`, permanently stalling `reward_per_token_stored` growth for every staker in that pool while reward tokens remain locked in the pool account. [2](#0-1) 

## Finding Description
`reward_per_token` computes the incremental reward-per-token using floor division: `reward_rate_per_block.ensure_mul(blocks_elapsed).ensure_mul(PRECISION_SCALING_FACTOR).ensure_div(total_tokens_staked)`. [3](#0-2)  `derive_rewards` divides the per-staker delta back down by the same constant. [4](#0-3)  The `stake()` extrinsic only requires `ensure_signed(origin)` — there is no admin/governance gate, no cap on `amount`, and `Self::update_pool_and_staker_rewards` is invoked (and thus `last_update_block` reset to the current block) on every call, including calls with small or zero amounts. [2](#0-1)  This lets any unprivileged staker both (a) inflate `total_tokens_staked` past the truncation threshold relative to `reward_rate_per_block * PRECISION_SCALING_FACTOR`, and (b) repeatedly reset `last_update_block` (e.g., via minimal stakes each block) to keep `blocks_elapsed` small, guaranteeing the numerator floors to zero on every subsequent update, permanently losing the reward accrued for those blocks (not merely delaying it). Existing guards do not prevent this: `create_pool` only checks asset existence and expiry-in-future, with no relationship enforced between `reward_rate_per_block` and expected `total_tokens_staked`. [5](#0-4)  `set_pool_reward_rate_per_block` only blocks rate *decreases* by the admin and does nothing to bound `total_tokens_staked`. [6](#0-5)  Reward tokens deposited into the pool account are only recoverable via `cleanup_pool`, which requires zero stakers (`ensure!(stakers.is_none(), Error::<T>::NonEmptyPool)`), so as long as any staker remains, admin cannot reclaim the stuck funds either. [7](#0-6) 

## Impact Explanation
This causes a permanent reward/fund-lock condition: reward tokens deposited into the pool remain in the pool's sub-account, unclaimable by legitimate stakers because `reward_per_token_stored` stops increasing, and unrecoverable by the admin while any staker remains in the pool (`cleanup_pool` requires an empty staker set). This matches the allowed "permanent user-fund or bridge-state lock" impact category, reachable entirely through ordinary, unprivileged pool participation without governance or admin cooperation.

## Likelihood Explanation
Any account holding the staked asset can trigger this without collusion or privileged access: pools with modest `reward_rate_per_block` relative to a large or growing `total_tokens_staked` are naturally exposed, and a single large `stake()` call (or repeated frequent small-stake calls that reset `last_update_block`) is sufficient to cross the truncation threshold, given `PRECISION_SCALING_FACTOR` is fixed at only `4096` regardless of asset decimals or pool configuration.

## Recommendation
Replace the fixed `PRECISION_SCALING_FACTOR: u16 = 4096` in `substrate/frame/asset-rewards/src/lib.rs` with a materially larger precision multiplier (ideally `u128`-scale, ideally per-pool-configurable or decimals-aware), and add an explicit check in `reward_per_token` (or as a rejection guard in `stake`) so that when the intermediate numerator would truncate to zero relative to `total_tokens_staked`, reward accrual either accumulates a carried remainder instead of discarding it, or the operation is rejected rather than silently zeroing the reward for that window.

## Proof of Concept
1. Root/CreatePoolOrigin calls `create_pool` with a modest `reward_rate_per_block` (e.g., `100`) for a staked asset with many decimals. [8](#0-7) 
2. An unprivileged account calls `stake(origin, pool_id, amount)` with `amount` such that `total_tokens_staked > reward_rate_per_block * PRECISION_SCALING_FACTOR (4096)` relative to the number of blocks expected between updates. [2](#0-1) 
3. On the next call to `reward_per_token` (triggered by any `stake`/`unstake`/reward-claim interaction), `reward_rate_per_block * blocks_elapsed * 4096 / total_tokens_staked` evaluates to `0` due to floor division, and `reward_per_token_stored` does not increase. [3](#0-2) 
4. Repeat small `stake()` calls each block to keep resetting `last_update_block`/`blocks_elapsed` low, ensuring the numerator never grows enough to escape truncation, while reward tokens continue to be deposited via `deposit_reward_tokens` and remain stuck in the pool account, unreachable by stakers and unreachable by admin via `cleanup_pool` while stakers remain. [7](#0-6)

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L449-467)
```rust
		pub fn create_pool(
			origin: OriginFor<T>,
			staked_asset_id: Box<T::AssetId>,
			reward_asset_id: Box<T::AssetId>,
			reward_rate_per_block: T::Balance,
			expiry: DispatchTime<BlockNumberFor<T>>,
			admin: Option<T::AccountId>,
		) -> DispatchResult {
			let creator = T::CreatePoolOrigin::ensure_origin(origin)?;
			<Self as RewardsPool<_>>::create_pool(
				&creator,
				*staked_asset_id,
				*reward_asset_id,
				reward_rate_per_block,
				expiry,
				&admin.unwrap_or_else(|| creator.clone()),
			)?;
			Ok(())
		}
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L700-704)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			ensure!(pool_info.admin == who, BadOrigin);

			let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
			ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L803-809)
```rust
			Ok(pool_info.reward_per_token_stored.ensure_add(
				pool_info
					.reward_rate_per_block
					.ensure_mul(rewardable_blocks_elapsed.into())?
					.ensure_mul(PRECISION_SCALING_FACTOR.into())?
					.ensure_div(pool_info.total_tokens_staked)?,
			)?)
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L815-824)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L850-858)
```rust
	) -> Result<PoolId, DispatchError> {
		// Ensure the assets exist.
		ensure!(T::Assets::asset_exists(staked_asset_id.clone()), Error::<T>::NonExistentAsset);
		ensure!(T::Assets::asset_exists(reward_asset_id.clone()), Error::<T>::NonExistentAsset);

		// Check the expiry block.
		let now = T::BlockNumberProvider::current_block_number();
		let expiry_block = expiry.evaluate(now);
		ensure!(expiry_block > now, Error::<T>::ExpiryBlockMustBeInTheFuture);
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
