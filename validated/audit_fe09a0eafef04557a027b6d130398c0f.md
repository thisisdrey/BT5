## Finding

The Solidity report's root cause — a "last update" checkpoint that is computed but never persisted to storage, so every later call recomputes elapsed time/accrued value from the same stale checkpoint and over-credits rewards — has a direct, concrete analog in `pallet-asset-rewards`'s `harvest_rewards` extrinsic.

### Title
`harvest_rewards` never persists the recomputed pool checkpoint, letting anyone repeatedly double-count elapsed blocks and over-mint reward-per-token for a pool - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`Pallet::update_pool_and_staker_rewards` derives a fresh `reward_per_token_stored` and advances `last_update_block` to the current block, but callers are responsible for persisting the returned `PoolInfo` back into the `Pools` storage map. `stake` and `unstake` both do this via `Pools::<T>::insert(pool_id, pool_info)`. `harvest_rewards` computes the same updated `pool_info` but only uses it to authorize the token transfer — it never calls `Pools::<T>::insert` to save the new `last_update_block` / `reward_per_token_stored` back to storage. [1](#0-0) 

Compare with `stake`, which persists the pool update: [2](#0-1) 

and `unstake`, which does the same: [3](#0-2) 

The shared helper that computes the checkpoint that must be persisted: [4](#0-3) 

### Finding Description
`reward_per_token()` derives the pool's accumulated reward-per-token by multiplying `reward_rate_per_block` by the number of blocks elapsed since `pool_info.last_update_block`: [5](#0-4) 

Because `harvest_rewards` never writes the returned `pool_info` (with its advanced `last_update_block` and increased `reward_per_token_stored`) back into `Pools::<T>`, the on-chain pool checkpoint stays exactly where it was before the harvest — analogous to `OmnipoolController::lastWeightUpdate` never being updated after `updateWeights`. Every subsequent call that reads the pool (another `stake`, `unstake`, or `harvest_rewards`, even by a different staker) recomputes `reward_per_token` using the same stale `last_update_block`/`reward_per_token_stored`, causing the same block interval to be counted again into the pool-wide `reward_per_token` accumulator. Each staker's own `reward_per_token_paid` marker (persisted in `PoolStakers`) advances correctly for that staker, but the pool-level accumulator keeps re-issuing rewards for windows that were already paid out to a previous harvester, inflating total rewards distributed by the pool beyond `reward_rate_per_block × elapsed_since_pool_creation`.

### Impact Explanation
This lets an unprivileged staker (or any two stakers acting independently and without collusion) cause the pool to over-distribute its `reward_asset_id` tokens beyond the amount funded/intended by the pool admin via `reward_rate_per_block`. Because rewards are paid via `T::Assets::transfer` from the pool's own account, repeated exploitation can drain the pool's reward-asset balance faster than the admin funded it, starving later, legitimate claimants (fund loss / broken payout accounting), which matches the "reward theft/duplicate settlement" class called out in the impact gate. This requires no privileged role, malicious validator, or governance action — only calling the public `harvest_rewards` extrinsic in the pool's normal operating flow.

### Likelihood Explanation
High. `harvest_rewards` is a normal, frequently-used public extrinsic; a caller can trigger it for themselves as soon as they have any staked position and non-zero pending reward. Multiple stakers or repeated calls interleaved with `stake`/`unstake` by other participants are enough to trigger the double-counted window — no special preconditions, timing games, or front-running needed.

### Recommendation
`harvest_rewards` must persist the pool-level checkpoint exactly like `stake`/`unstake` do: after computing `(pool_info, staker_info) = Self::update_pool_and_staker_rewards(...)`, call `Pools::<T>::insert(pool_id, pool_info)` before/along with the reward transfer and `PoolStakers` update, so `last_update_block` and `reward_per_token_stored` are advanced in storage on every path that reads them.

### Proof of Concept
1. Admin creates a pool with `reward_rate_per_block = R`, funds it with reward tokens.
2. Staker A stakes `X` tokens at block `M`.
3. At block `N`, Staker A calls `harvest_rewards`. `update_pool_and_staker_rewards` computes `reward_per_token = X + R*(N-M)/total_staked` and pays Staker A accordingly; `PoolStakers` is updated with the new `reward_per_token_paid`, but `Pools::<T>::get(pool_id).last_update_block` is still `M` and `reward_per_token_stored` is still the pre-harvest value (no `Pools::insert` call in `harvest_rewards`).
4. Staker B stakes at block `N2 > N`. `stake` calls `update_pool_and_staker_rewards`, which recomputes `reward_per_token` using elapsed `N2 - M` (not `N2 - N`) against the pool's stale stored value — re-including the `[M, N]` interval that was already paid out to Staker A in step 3.
5. Total rewards now distributable from the pool (sum across all stakers' claims) exceed `R * (N2 - M)`, i.e., more than the pool was designed to emit for that block range, draining the reward-asset balance disproportionately faster than intended.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L473-502)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L754-784)
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

		/// Computes update pool reward state.
		///
		/// Should be called every time the pool is adjusted, and a staker is not involved.
		///
		/// Returns the updated pool and staker info.
		///
		/// NOTE: this function has no side-effects. Side-effects such as storage modifications are
		/// the responsibility of the caller.
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
