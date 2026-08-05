Confirmed. The code exactly matches the claim: `update_pool_rewards` unconditionally sets `new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number()` with no cap at `expiry_block` at `substrate/frame/asset-rewards/src/lib.rs:780`, and `reward_per_token` computes `Self::last_block_reward_applicable(pool_info.expiry_block).ensure_sub(pool_info.last_update_block)?` at lines 795-796, using `ensure_sub` which errors (rather than saturates) on underflow. `last_block_reward_applicable` clamps to `expiry_block` once `now >= expiry_block` at lines 826-833. `unstake` and `harvest_rewards` both explicitly permit calls after expiry via `ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin)` at lines 526 and 580, so a legitimate post-expiry call advances `last_update_block` past `expiry_block`, and any subsequent call to `stake`/`unstake`/`harvest_rewards` (all of which call `update_pool_and_staker_rewards` → `reward_per_token` at lines 754-765, 787-810) will underflow and revert permanently, with no recovery since `cleanup_pool` requires zero stakers (line 703-704) but stakers can no longer `unstake` to exit.

Audit Report

## Title
`asset-rewards` pool becomes permanently unusable after expiry due to `last_update_block` exceeding `expiry_block`, causing arithmetic underflow that reverts `stake`/`unstake`/`harvest_rewards` forever - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`update_pool_rewards` unconditionally sets `PoolInfo::last_update_block` to the current block number with no cap at `expiry_block`, while `reward_per_token` computes `last_block_reward_applicable(expiry_block).ensure_sub(last_update_block)`. Once any call occurs after expiry, `last_update_block` exceeds `expiry_block`, and the next call's `ensure_sub` underflows, permanently reverting `stake`, `unstake`, and `harvest_rewards` for that pool.

## Finding Description
`update_pool_rewards` writes `new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number()` unconditionally [1](#0-0) . `reward_per_token` derives elapsed blocks as `last_block_reward_applicable(pool_info.expiry_block).ensure_sub(pool_info.last_update_block)?`, using `ensure_sub` which errors on underflow rather than saturating [2](#0-1) . `last_block_reward_applicable` clamps `now` to `expiry_block` once `now >= expiry_block` [3](#0-2) .

`unstake` and `harvest_rewards` explicitly allow the staker to call after expiry via `ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin)` [4](#0-3) [5](#0-4) , and `stake` also calls `update_pool_and_staker_rewards` unconditionally [6](#0-5) . A single legitimate post-expiry call bumps `last_update_block` past `expiry_block` in storage. Any subsequent call to these three dispatchables invokes `update_pool_and_staker_rewards` → `reward_per_token` [7](#0-6) , whose `ensure_sub` now underflows and returns `ArithmeticError`, reverting the extrinsic. `cleanup_pool` requires zero stakers to proceed [8](#0-7) , but stakers can no longer call `unstake` to reach zero, so there is no recovery path.

## Impact Explanation
This permanently locks staked tokens and unclaimed reward tokens for the affected pool: once `last_update_block > expiry_block`, every future `stake`, `unstake`, and `harvest_rewards` call on that pool reverts deterministically, and `cleanup_pool` cannot succeed since stakers can't exit. This matches the "permanent user-fund lock" impact category in the Polkadot SDK impact gate.

## Likelihood Explanation
High and fully unprivileged: any staker legitimately calling `unstake` or `harvest_rewards` after `expiry_block` — an intended, documented usage path, not an edge case — triggers the state corruption; every later interaction by any account then fails deterministically with no special conditions or race required.

## Recommendation
Cap `last_update_block` at `expiry_block` in `update_pool_rewards`, e.g. set it to `Self::last_block_reward_applicable(pool_info.expiry_block)` instead of the raw `current_block_number()`, ensuring `last_update_block` never exceeds `expiry_block` once the pool has expired.

## Proof of Concept
1. Create a pool with `expiry_block = E` and nonzero `reward_rate_per_block`; a staker stakes before `E`.
2. At block `E+1`, the staker calls `unstake` for a partial amount — succeeds, and `update_pool_rewards` sets `pool_info.last_update_block = E+1` in storage (`substrate/frame/asset-rewards/src/lib.rs:780`), which is now `> expiry_block`.
3. At any block `now2 >= E+1`, the same or another authorized caller invokes `unstake` or `harvest_rewards` again: `reward_per_token` computes `last_block_reward_applicable(E) = E`, then `E.ensure_sub(E+1)` underflows, returning `ArithmeticError::Underflow`, and the extrinsic reverts.
4. All subsequent calls to `stake`, `unstake`, or `harvest_rewards` for this pool revert identically, permanently locking remaining staked tokens and unclaimed rewards.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L476-480)
```rust
			// Always start by updating staker and pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L524-530)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L703-704)
```rust
			let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
			ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L794-801)
```rust
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
