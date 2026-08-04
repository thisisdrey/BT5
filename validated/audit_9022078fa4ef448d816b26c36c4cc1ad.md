### Title
Permanent staked-fund lock in `pallet-asset-rewards` via unclamped `last_update_block` causing perpetual arithmetic underflow after pool expiry - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes reward accrual by capping "now" to the pool's `expiry_block` when computing elapsed blocks, but when persisting the pool state it stores the *uncapped* current block number as `last_update_block`. Once the chain progresses past `expiry_block` and any pool interaction occurs, `last_update_block` becomes permanently greater than `expiry_block`. Every subsequent call that recomputes rewards then performs `expiry_block.ensure_sub(last_update_block)`, which underflows and returns `ArithmeticError::Underflow`. Because `unstake()`, `harvest_rewards()`, and even the admin recovery function `set_pool_expiry_block()` all invoke this calculation before doing anything else, the pool becomes permanently stuck: stakers can never withdraw their staked assets, matching the "lock the LP position forever" pattern from the reported Uniswap V3 Staker bug.

### Finding Description
Reward accrual is computed in `reward_per_token`: [1](#0-0) 

```rust
pub(super) fn reward_per_token(pool_info: &PoolInfoFor<T>) -> Result<T::Balance, DispatchError> {
    if pool_info.total_tokens_staked.is_zero() {
        return Ok(pool_info.reward_per_token_stored);
    }
    let rewardable_blocks_elapsed: u32 =
        match Self::last_block_reward_applicable(pool_info.expiry_block)
            .ensure_sub(pool_info.last_update_block)?
            .try_into() { ... };
    ...
}
```

`last_block_reward_applicable` deliberately caps the "now" value to `expiry_block`: [2](#0-1) 

However, when the newly computed reward is persisted, `update_pool_rewards` stores the **uncapped** current block number, not the capped value used in the calculation: [3](#0-2) 

```rust
pub fn update_pool_rewards(pool_info: &PoolInfoFor<T>, reward_per_token: T::Balance) -> Result<PoolInfoFor<T>, DispatchError> {
    let mut new_pool_info = pool_info.clone();
    new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number();
    new_pool_info.reward_per_token_stored = reward_per_token;
    Ok(new_pool_info)
}
```

Once any interaction (stake/unstake/harvest/rate change/expiry change) happens at `now > expiry_block`, `last_update_block` is set to `now`, which is strictly greater than `expiry_block`. On every future call (as long as the chain keeps advancing, which it always does), `last_block_reward_applicable(expiry_block)` returns `min(now', expiry_block) = expiry_block` (since `now' > expiry_block`), and then:

```rust
expiry_block.ensure_sub(last_update_block)  // expiry_block < last_update_block → underflow
```

`ensure_sub` returns `Err(ArithmeticError::Underflow)`, propagated via `?` out of `reward_per_token`, `update_pool_rewards`, and `update_pool_and_staker_rewards`.

All the pallet's public extrinsics that touch a pool with nonzero `total_tokens_staked` call this chain first:
- `unstake` — [4](#0-3) 
- `harvest_rewards` — [5](#0-4) 
- `set_pool_reward_rate_per_block` — [6](#0-5) 
- `set_pool_expiry_block` — [7](#0-6) 

Critically, `set_pool_expiry_block` — the only admin function that could otherwise "fix" the pool by pushing `expiry_block` beyond `last_update_block` — itself calls `reward_per_token` **before** updating `expiry_block`, so it fails with the same underflow. There is no recovery path once the corruption occurs.

### Impact Explanation
Any staker with tokens locked in a pool at the time it becomes "poisoned" (first post-expiry interaction) can never call `unstake()` successfully again — every call reverts with `ArithmeticError::Underflow`. This is a permanent fund lock of user-owned staked assets (frozen via `T::AssetsFreezer`), with no admin or governance recovery path since `set_pool_expiry_block` is equally blocked. This matches the "Balances/assets/pools must conserve value and settle exactly once" and "permanent user-fund lock" impact categories.

### Likelihood Explanation
This requires no privileged actor, malicious peer, or governance action — any ordinary user interaction (e.g., a late `harvest_rewards` call by any staker, or even the pool creator's own `set_pool_reward_rate_per_block`) after the pool's `expiry_block` has passed is sufficient to trigger the corruption, since these are all unprivileged, permissionless-to-call once a pool exists. Given that pools are explicitly designed to run to an expiry and then stop, and nothing prevents post-expiry calls (they're only blocked at the freeze/asset level, not at the reward computation level), this condition will be reached in essentially any pool where a staker doesn't unstake before or exactly at expiry.

### Recommendation
In `update_pool_rewards`, clamp the stored `last_update_block` the same way `reward_per_token` clamps "now" for the elapsed-blocks calculation:
```rust
new_pool_info.last_update_block = Self::last_block_reward_applicable(pool_info.expiry_block);
```
This keeps `last_update_block <= expiry_block` always, preserving the invariant that `last_block_reward_applicable(expiry_block).ensure_sub(last_update_block)` never underflows. Additionally, consider using `saturating_sub`/`Zero::zero()` fallback defensively for this specific computation so that even edge cases fail safe (compute zero rewardable blocks) instead of reverting the whole extrinsic.

### Proof of Concept
1. Admin creates a pool with `expiry_block = E` via `create_pool`.
2. A staker calls `stake(pool_id, amount)` at some block `< E`, so `total_tokens_staked > 0`.
3. Advance the chain past `E` (e.g., `now = E + 10`).
4. Any account calls `harvest_rewards(pool_id, None)` (or the staker calls `unstake`) — this succeeds (rewards correctly capped to `E`), but `update_pool_rewards` now sets `pool_info.last_update_block = E + 10` (uncapped) while `expiry_block` remains `E`.
5. Advance the chain further (`now = E + 20`).
6. The staker calls `unstake(pool_id, amount, None)`. Internally: `reward_per_token` computes `last_block_reward_applicable(E) = E`, then `E.ensure_sub(E + 10)` → underflow → `Err(ArithmeticError::Underflow)` → `unstake` fails.
7. Every subsequent call to `unstake`, `harvest_rewards`, `set_pool_reward_rate_per_block`, or `set_pool_expiry_block` for this pool fails identically, forever. The staker's tokens remain frozen in the pool with no recovery mechanism.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-530)
```rust
		#[pallet::call_index(2)]
		pub fn unstake(
			origin: OriginFor<T>,
			pool_id: PoolId,
			amount: T::Balance,
			staker: Option<T::AccountId>,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let staker = staker.unwrap_or(caller.clone());

			// Always start by updating the pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L568-585)
```rust
		#[pallet::call_index(3)]
		pub fn harvest_rewards(
			origin: OriginFor<T>,
			pool_id: PoolId,
			staker: Option<T::AccountId>,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let staker = staker.unwrap_or(caller.clone());

			// Always start by updating the pool and staker rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info =
				PoolStakers::<T>::get(pool_id, &staker).ok_or(Error::<T>::NonExistentStaker)?;
			let (pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L940-963)
```rust
	fn set_pool_expiry_block(
		admin: &T::AccountId,
		pool_id: PoolId,
		new_expiry: DispatchTime<BlockNumberFor<T>>,
	) -> DispatchResult {
		let now = T::BlockNumberProvider::current_block_number();
		let new_expiry_block = new_expiry.evaluate(now);
		ensure!(new_expiry_block > now, Error::<T>::ExpiryBlockMustBeInTheFuture);

		let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
		ensure!(pool_info.admin == *admin, BadOrigin);
		ensure!(new_expiry_block > pool_info.expiry_block, Error::<T>::ExpiryCut);

		// Always start by updating the pool rewards.
		let reward_per_token = Self::reward_per_token(&pool_info)?;
		let mut pool_info = Self::update_pool_rewards(&pool_info, reward_per_token)?;

		pool_info.expiry_block = new_expiry_block;
		Pools::<T>::insert(pool_id, pool_info);

		Self::deposit_event(Event::PoolExpiryBlockModified { pool_id, new_expiry_block });

		Ok(())
	}
```
