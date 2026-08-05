Audit Report

## Title
`asset-rewards` pool becomes permanently unusable after expiry due to `last_update_block` exceeding `expiry_block`, causing arithmetic underflow that reverts `stake`/`unstake`/`harvest_rewards` forever - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`update_pool_rewards` unconditionally sets `PoolInfo::last_update_block` to the current block number with no cap at `expiry_block`, while `reward_per_token` computes `last_block_reward_applicable(expiry_block).ensure_sub(last_update_block)` using an error-on-underflow subtraction. Once any legitimate post-expiry interaction bumps `last_update_block` above `expiry_block`, every subsequent call to `stake`, `unstake`, or `harvest_rewards` on that pool underflows and reverts, permanently locking staked funds and unclaimed rewards.

## Finding Description
`update_pool_rewards` writes `new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number()` unconditionally, without clamping to `pool_info.expiry_block`: [1](#0-0) 

`reward_per_token` computes elapsed rewardable blocks as `last_block_reward_applicable(pool_info.expiry_block).ensure_sub(pool_info.last_update_block)`, which is only reached when `total_tokens_staked` is nonzero, and returns an `ArithmeticError` on underflow via `ensure_sub`: [2](#0-1) 

`last_block_reward_applicable` clamps `now` to `pool_expiry_block`, meaning it can never return a value greater than `expiry_block`: [3](#0-2) 

The exploit path: `unstake` and `harvest_rewards` both explicitly permit calls after expiry via `ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin)`, so a staker calling either after `expiry_block` is fully legitimate: [4](#0-3) [5](#0-4) 

When such a call succeeds (using the still-valid old `last_update_block <= expiry_block`), `update_pool_rewards` stores `last_update_block = now > expiry_block` into `Pools` storage via `Pools::<T>::insert(pool_id, pool_info)`: [6](#0-5) 

On any later call to `stake`, `unstake`, or `harvest_rewards` for the same pool (as long as `total_tokens_staked` remains nonzero, e.g. after a partial unstake), `update_pool_and_staker_rewards` invokes `reward_per_token`, which computes `expiry_block.ensure_sub(last_update_block)` where `last_update_block > expiry_block`, underflowing and returning `Err(ArithmeticError::Underflow)` that propagates via `?` up through `stake`/`unstake`/`harvest_rewards`, reverting the extrinsic: [7](#0-6) 

There is no code path that caps `last_update_block` at `expiry_block`, so this corrupted state is permanent once introduced.

## Impact Explanation
This is a permanent user-fund lock: once `Pools::<PoolId>::last_update_block` exceeds `expiry_block` for a pool with nonzero `total_tokens_staked`, every subsequent `stake`, `unstake`, and `harvest_rewards` call for that pool deterministically reverts with an arithmetic underflow. Stakers cannot withdraw their staked tokens or harvest earned rewards, and `cleanup_pool` cannot proceed since it requires zero remaining stakers (`ensure!(stakers.is_none(), Error::<T>::NonEmptyPool)`), leaving funds permanently locked in the pool account. This matches the "permanent user-fund lock" impact class in the accepted impact gate.

## Likelihood Explanation
High. No privileged actor is needed: any staker performing an explicitly-permitted post-expiry partial `unstake` (leaving `total_tokens_staked` nonzero) or `harvest_rewards` call triggers the state corruption. Every subsequent interaction with that pool by any account then deterministically reverts, making this trivially reachable and repeatable through normal, documented usage patterns (post-expiry unstake/harvest).

## Recommendation
Cap `last_update_block` at `expiry_block` in `update_pool_rewards`, e.g. set it to `Self::last_block_reward_applicable(pool_info.expiry_block)` instead of the raw `current_block_number()`, ensuring `last_update_block` never exceeds `expiry_block`. Alternatively, use saturating subtraction consistently in `reward_per_token` so a no-op (zero elapsed blocks) results instead of an error when `now >= expiry_block`.

## Proof of Concept
1. Admin creates a pool with `expiry_block = E` and nonzero `reward_rate_per_block`; staker A stakes `amount` tokens before `E`.
2. At block `E+1` (`now > expiry_block`), staker A calls `unstake` with a partial amount, leaving `total_tokens_staked > 0`. This succeeds: `reward_per_token()` computes `E.ensure_sub(old_last_update_block)` (valid since `old_last_update_block <= E`), then `update_pool_rewards` sets and stores `pool_info.last_update_block = E+1`.
3. At block `E+2` (or later), staker A (or another staker) calls `unstake` again or `harvest_rewards`. `reward_per_token()` computes `last_block_reward_applicable(E) = E`, then `E.ensure_sub(E+1)`, which underflows and returns `Err(ArithmeticError::Underflow)`.
4. The dispatchable reverts. All future `stake`/`unstake`/`harvest_rewards` calls on this pool underflow identically, permanently locking remaining staked tokens and unclaimed rewards, and blocking `cleanup_pool` since stakers can never reach zero balance.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L524-530)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L543-545)
```rust
			// Update Pools.
			pool_info.total_tokens_staked.ensure_sub_assign(amount)?;
			Pools::<T>::insert(pool_id, pool_info);
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L578-585)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info =
				PoolStakers::<T>::get(pool_id, &staker).ok_or(Error::<T>::NonExistentStaker)?;
			let (pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L786-801)
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
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L826-833)
```rust
		fn last_block_reward_applicable(pool_expiry_block: BlockNumberFor<T>) -> BlockNumberFor<T> {
			let now = T::BlockNumberProvider::current_block_number();
			if now < pool_expiry_block {
				now
			} else {
				pool_expiry_block
			}
		}
```
