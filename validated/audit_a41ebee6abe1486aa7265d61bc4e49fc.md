Audit Report

## Title
Reward-per-block timer keeps advancing during zero-stake windows, permanently skipping rewards owed for that period - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
The `pallet-asset-rewards` `reward_per_token` function returns the stored `reward_per_token_stored` value unchanged whenever `total_tokens_staked` is zero, correctly avoiding a division by zero, but `update_pool_rewards` unconditionally advances `last_update_block` to the current block on every call regardless of whether stake was zero during the elapsed interval. [1](#0-0) [2](#0-1)  Because reward emission is a fixed rate tied to `expiry_block` via `last_block_reward_applicable`, any zero-stake interval's notional reward budget is permanently dropped rather than deferred to a future staker.

## Finding Description
`update_pool_and_staker_rewards`, which every pool-touching entry point calls before mutating state, computes `reward_per_token` and then persists it via `update_pool_rewards`: [3](#0-2) . `reward_per_token` correctly short-circuits when `total_tokens_staked.is_zero()`: [1](#0-0) , but `update_pool_rewards` writes `new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number()` unconditionally: [2](#0-1) . This entry point is used by `stake` (confirmed at lines 472-502, calling `update_pool_and_staker_rewards` before adjusting `total_tokens_staked`) [4](#0-3)  and by `set_pool_reward_rate_per_block` [5](#0-4) . Since `reward_rate_per_block` is a fixed emission rate bounded by `expiry_block` (via `last_block_reward_applicable`), any interval where `total_tokens_staked == 0` causes the "would-be" reward for that span to be permanently discarded — the next `reward_per_token` computation starts from the newly advanced `last_update_block`, not from when stake was last non-zero, and the pool's fixed `expiry_block` cap means the lost blocks are not recoverable later.

## Impact Explanation
This is a runtime accounting bug that compromises the pallet's intended reward-distribution behavior: pool admins fund pools expecting `reward_rate_per_block * (expiry_block - creation_block)` tokens to be fully distributed to stakers, but any zero-stake period (which is a normal, expected occurrence — e.g., before the first staker joins, or after all stakers fully withdraw) causes that portion of the reward budget to be permanently and silently lost rather than deferred. This matches the "runtime bugs that compromise intended behavior" category for reward/payout accounting in the impact gate.

## Likelihood Explanation
The bug is triggered purely through permissionless public extrinsics — no privileged actor, governance, or admin action is required. Every pool experiences this at least once, between `create_pool` and the first `stake` call, and any staker can reproduce it deterministically by fully unstaking (driving `total_tokens_staked` to zero) and staking again later. This is a deterministic, always-reachable code path rather than a race condition.

## Recommendation
Do not advance `last_update_block` past the point at which `total_tokens_staked` became zero. Either track the last block at which stake was non-zero and cap the checkpoint update to that, or skip advancing `last_update_block` entirely while `total_tokens_staked.is_zero()`, ensuring the reward-rate clock only progresses while stake exists.

## Proof of Concept
1. `create_pool` at block `B0` with `reward_rate_per_block = R`, `expiry_block = E`. `total_tokens_staked = 0`, `last_update_block` initialized at creation.
2. No staker exists until block `B1 > B0`. Alice calls `stake` at `B1`: `update_pool_and_staker_rewards` → `reward_per_token` returns the stored (unchanged) value because `total_tokens_staked.is_zero()` [6](#0-5) , then `update_pool_rewards` sets `last_update_block = B1` [7](#0-6) .
3. Rewards for `[B0, B1)` are never credited to Alice or any future staker.
4. Alice later calls `unstake` for her full amount at block `B2`, bringing `total_tokens_staked` back to zero; a new staker joins at `B3 > B2`. The interval `[B2, B3)` is likewise skipped, permanently shrinking the effective reward-earning window bounded by the fixed `expiry_block`.

### Citations

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L754-765)
```rust
		pub fn update_pool_and_staker_rewards(
			pool_info: &PoolInfoFor<T>,
			staker_info: &PoolStakerInfo<T::Balance>,
		) -> Result<(PoolInfoFor<T>, PoolStakerInfo<T::Balance>), DispatchError> {
			let reward_per_token = Self::reward_per_token(&pool_info)?;
			let pool_info = Self::update_pool_rewards(pool_info, reward_per_token)?;

			let mut new_staker_info = staker_info.clone();
			new_staker_info.rewards = Self::derive_rewards(&staker_info, &reward_per_token)?;
			new_staker_info.reward_per_token_paid = pool_info.reward_per_token_stored;
			return Ok((pool_info, new_staker_info));
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L787-792)
```rust
		pub(super) fn reward_per_token(
			pool_info: &PoolInfoFor<T>,
		) -> Result<T::Balance, DispatchError> {
			if pool_info.total_tokens_staked.is_zero() {
				return Ok(pool_info.reward_per_token_stored);
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
