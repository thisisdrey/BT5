## Title
`asset-rewards` pool becomes permanently unusable after expiry due to `last_update_block` exceeding `expiry_block`, causing arithmetic underflow that reverts `stake`/`unstake`/`harvest_rewards` forever - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
The reported Astaria bug is that `lien.last` gets bumped to `block.timestamp` even in expired/liquidation flows, and a later computation subtracts `lien.last` from an "end" value capped at expiry, causing an underflow revert that DoS's legitimate partial-payoff flows. The `pallet-asset-rewards` (FRAME Staking Rewards Pallet) has the exact same structural bug: `PoolInfo::last_update_block` is unconditionally set to `current_block_number()` on every pool interaction, even after the pool's `expiry_block` has passed, while `reward_per_token()` computes `last_block_reward_applicable(expiry_block).ensure_sub(last_update_block)`, which underflows once `last_update_block > expiry_block`.

### Finding Description
`update_pool_rewards` writes the pool's `last_update_block` to the current block number unconditionally, with no cap at `expiry_block`: [1](#0-0) 

`reward_per_token` computes elapsed "rewardable" blocks as the reward-applicable block (capped at `expiry_block`) minus `last_update_block`, using `ensure_sub`, which errors on underflow instead of saturating: [2](#0-1) 

`last_block_reward_applicable` clamps `now` to `expiry_block`: [3](#0-2) 

Once any interaction occurs after the pool has expired (`now > expiry_block`), `update_pool_rewards` sets `last_update_block = now`, which is now strictly greater than `expiry_block`. On the very next call to any function that invokes `update_pool_and_staker_rewards`, `last_block_reward_applicable(expiry_block)` returns `expiry_block` (since `now2 >= expiry_block`), and `expiry_block.ensure_sub(last_update_block)` underflows (`expiry_block < last_update_block`), returning an `ArithmeticError` that propagates through `?` up to the dispatchable and reverts the whole extrinsic.

The three public entrypoints that call `update_pool_and_staker_rewards` at the very start of their bodies — `stake`, `unstake`, and `harvest_rewards` — all become permanently unusable for that pool after this state is reached: [4](#0-3) [5](#0-4) [6](#0-5) 

Critically, both `unstake` and `harvest_rewards` explicitly permit the staker to call them even after expiry (`now > pool_info.expiry_block || caller == staker`), meaning this is a fully legitimate, expected, unprivileged post-expiry action that itself corrupts the pool state and locks it forever.

### Impact Explanation
Once `last_update_block` is bumped past `expiry_block` by any single post-expiry `stake`/`unstake`/`harvest_rewards` call (a completely normal, permitted user action), every subsequent call to any of these three dispatchables for that pool reverts with an arithmetic underflow. There is no recovery path in the pallet: `cleanup_pool` requires the pool to have zero stakers, but stakers can no longer call `unstake` to exit. This results in a permanent lock of user-staked funds and unclaimed reward tokens in the pool account, matching the "permanent user-fund lock" impact class.

### Likelihood Explanation
High. No privileged actor, governance, or malicious peer is required. Any staker legitimately calling `unstake` or `harvest_rewards` after the pool's `expiry_block` (a documented, intended usage pattern once a pool's incentive period ends) will trip this state; every subsequent interaction with the pool by any account then reverts deterministically.

### Recommendation
Cap `last_update_block` at `expiry_block` when the pool is being updated (i.e. set it to `last_block_reward_applicable(pool_info.expiry_block)` instead of the raw `current_block_number()`), or use `saturating_sub`/`min` semantics consistently in `reward_per_token` so that once `now >= expiry_block`, `last_update_block` never exceeds `expiry_block`.

### Proof of Concept
1. Admin creates a pool with `expiry_block = E` and a nonzero `reward_rate_per_block`; a staker stakes tokens before `E`.
2. At block `E+1` (`now > expiry_block`), the staker calls `unstake` for a partial amount. This call succeeds: `reward_per_token()` computes `expiry_block.ensure_sub(old_last_update_block)` (still valid since `old_last_update_block <= expiry_block`), then `update_pool_rewards` sets `pool_info.last_update_block = E+1` in storage (`substrate/frame/asset-rewards/src/lib.rs:780`).
3. At any later block `now2 >= E+1` (e.g. `E+2`), the same or any other staker calls `unstake` again (to withdraw the remainder) or `harvest_rewards`. `update_pool_and_staker_rewards` → `reward_per_token()` computes `last_block_reward_applicable(E) = E`, then `E.ensure_sub(E+1)`, which underflows and returns `Err(ArithmeticError::Underflow)`.
4. The dispatchable returns this error and reverts. Every future call to `stake`, `unstake`, or `harvest_rewards` on this pool will hit the same underflow and revert forever, permanently locking the staker's remaining staked tokens and unclaimed rewards in the pool account.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-560)
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

			// Update PoolStakers.
			staker_info.amount.ensure_sub_assign(amount)?;

			if staker_info.amount.is_zero() && staker_info.rewards.is_zero() {
				PoolStakers::<T>::remove(&pool_id, &staker);
			} else {
				PoolStakers::<T>::insert(&pool_id, &staker, staker_info);
			}

			// Emit event.
			Self::deposit_event(Event::Unstaked { caller, staker, pool_id, amount });

			Ok(())
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L568-615)
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

			// Transfer unclaimed rewards from the pool to the staker.
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&pool_info.account,
				&staker,
				staker_info.rewards,
				// Could kill the account, but only if the pool was already almost empty.
				Preservation::Expendable,
			)?;

			// Emit event.
			Self::deposit_event(Event::RewardsHarvested {
				caller,
				staker: staker.clone(),
				pool_id,
				amount: staker_info.rewards,
			});

			// Reset staker rewards.
			staker_info.rewards = 0u32.into();

			if staker_info.amount.is_zero() {
				PoolStakers::<T>::remove(&pool_id, &staker);
			} else {
				PoolStakers::<T>::insert(&pool_id, &staker, staker_info);
			}

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
