## Title
Post-expiry pool interaction corrupts `PoolInfo.last_update_block`, permanently bricking stake/unstake and locking staked funds - (`substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`pallet-asset-rewards` computes reward accrual by capping the "last rewardable block" at `pool_info.expiry_block`, but `update_pool_rewards` unconditionally stamps `last_update_block` with the *current* block number instead of the same capped value. As soon as any permissionless post-expiry interaction (`stake` or `unstake`) is persisted, `last_update_block` ends up **greater than** `expiry_block`. Every subsequent reward computation then underflows and errors out, permanently blocking `stake`, `unstake`, `harvest_rewards`, and even the admin's own repair calls (`set_pool_expiry_block`, `set_pool_reward_rate_per_block`) — because they all call the same broken `reward_per_token` first. This is the same class of bug as the Y2K report: a time/expiry boundary that is supposed to gate a reward/value window is not consistently enforced across all code paths, but here the consequence is a **permanent lock of staked funds** rather than free rewards.

## Finding Description
`reward_per_token` caps the elapsed reward window using `last_block_reward_applicable`, which returns `min(now, expiry_block)`: [1](#0-0) 

That capped value is then used to compute `rewardable_blocks_elapsed` via a checked subtraction against `pool_info.last_update_block`: [2](#0-1) 

However, `update_pool_rewards` — called right after `reward_per_token` in every pool-mutating path — sets `last_update_block` to the **raw, uncapped** current block number, not to the same `last_block_reward_applicable(expiry_block)` value used for the reward math: [3](#0-2) 

`unstake` is explicitly designed to be permissionless once the pool has expired (`now > pool_info.expiry_block || caller == staker`), and it persists the (now corrupted) `pool_info` back to storage: [4](#0-3) 

`stake` has no expiry check at all and also persists `pool_info`: [5](#0-4) 

Once `last_update_block > expiry_block` is committed to `Pools` storage, every future call to `reward_per_token` for that pool computes `last_block_reward_applicable(expiry_block)` (which stays pinned at `expiry_block` since `now > expiry_block`) minus the now-larger `last_update_block`, producing an arithmetic underflow that `ensure_sub` turns into a `DispatchError`. This error propagates through `update_pool_and_staker_rewards` used by `stake`, `unstake`, and `harvest_rewards`: [6](#0-5) 

Critically, the admin's own "fix" entry points also call `reward_per_token` on the corrupted `pool_info` *before* applying any change, so they fail identically and cannot repair the pool: [7](#0-6) [8](#0-7) 

The only path that doesn't touch `reward_per_token`, `cleanup_pool`, requires the pool to have zero stakers, which can never occur once every unstake attempt reverts: [9](#0-8) 

## Impact Explanation
Once triggered, `total_tokens_staked` can never reach zero (no one can unstake), so `reward_per_token`'s early-return short-circuit for empty pools never applies, and the pool is permanently stuck. All assets frozen via `T::AssetsFreezer::increase_frozen` for that pool's stakers become unrecoverable: `unstake` (the only path that calls `decrease_frozen`) always errors, `harvest_rewards` always errors, and the admin cannot repair the pool through the exposed extrinsics. This matches the "permanent user-fund ... lock" impact category directly, and requires no privileged actor, malicious validator, or governance action — it is triggered by ordinary, intended usage (post-expiry unstake is explicitly permissionless by design).

## Likelihood Explanation
High. Any pool that reaches its `expiry_block` while still holding stakers will hit this the moment the *first* `stake` or `unstake` call lands after expiry — which is the expected, encouraged behavior (`unstake` is deliberately made permissionless past expiry so anyone can help wind pools down). No attacker coordination, race condition, or special asset configuration is required; a single ordinary transaction after expiry corrupts the pool for everyone.

## Recommendation
In `update_pool_rewards`, set `last_update_block` to the same capped value used for reward accounting instead of the raw current block number:
```rust
new_pool_info.last_update_block =
    Self::last_block_reward_applicable(pool_info.expiry_block);
```
This keeps `last_update_block` bounded by `expiry_block`, so `reward_per_token` never underflows for post-expiry calls, and admin repair calls (`set_pool_expiry_block`, `set_pool_reward_rate_per_block`) remain callable. Additionally, `stake` should probably reject staking into an already-expired pool, mirroring the analogous check already present in `unstake`/`harvest_rewards`.

## Proof of Concept
1. Admin calls `create_pool` with `expiry = DispatchTime::At(100)`; pool `id=0` created, `last_update_block = 0`.
2. Staker `A` calls `stake(0, 1000)` at block 10 — `Pools[0].last_update_block` becomes `10` (≤ 100, fine).
3. No further interaction happens until block `150` (past expiry `100`).
4. Staker `A` calls `unstake(0, 500, None)`. Guard passes (`caller == staker`). `reward_per_token` computes correctly using `last_block_reward_applicable(100) = 100` and `last_update_block = 10` → `90` blocks, no error. `update_pool_rewards` then sets `Pools[0].last_update_block = 150` (current block, **> expiry_block 100**) and this is persisted via `Pools::<T>::insert`.
5. Any later block (e.g. `160`), staker `A` (or anyone, since `now > expiry_block`) calls `unstake(0, 500, None)` again. `reward_per_token` computes `last_block_reward_applicable(100) = 100`, then `100.ensure_sub(150)` → arithmetic underflow → `Err`. The extrinsic fails.
6. From this point, `stake`, `unstake`, and `harvest_rewards` on pool `0` fail for every account forever; `set_pool_expiry_block`/`set_pool_reward_rate_per_block` also fail for the same reason, so the admin cannot recover the pool. `cleanup_pool` is unreachable because stakers can never be removed. Staker `A`'s remaining 500 staked tokens (and any other staker's tokens) are permanently frozen.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-545)
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

			// Check the staker has enough staked tokens.
			ensure!(staker_info.amount >= amount, Error::<T>::NotEnoughTokens);

			// Unfreeze staker assets.
			T::AssetsFreezer::decrease_frozen(
				pool_info.staked_asset_id.clone(),
				&FreezeReason::Staked.into(),
				&staker,
				amount,
			)?;

			// Update Pools.
			pool_info.total_tokens_staked.ensure_sub_assign(amount)?;
			Pools::<T>::insert(pool_id, pool_info);
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L696-729)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L786-809)
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
