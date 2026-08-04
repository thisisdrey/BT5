## Finding

The requested local analog exists in `pallet-asset-rewards`'s `harvest_rewards` extrinsic.

### Title
`harvest_rewards` never persists the recomputed `PoolInfo` (`reward_per_token_stored`, `last_update_block`), causing stale time/reward-per-token state and reward miscalculation - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet_asset_rewards::Pallet::harvest_rewards` calls `update_pool_and_staker_rewards` to compute a fresh `pool_info` (with an updated `reward_per_token_stored` and `last_update_block`) and a fresh `staker_info`, but only writes the staker side back to storage via `PoolStakers::<T>::insert`/`remove`. The recomputed `pool_info` is used locally (for the asset transfer) and then discarded — `Pools::<T>::insert(pool_id, pool_info)` is never called, unlike the sibling extrinsics `stake` and `unstake`, which both explicitly persist the updated pool state. [1](#0-0) [2](#0-1) 

### Finding Description
The pool's time-weighted reward accounting relies on `Pools::<T>::get(pool_id).last_update_block` and `.reward_per_token_stored` being advanced every time rewards accrue, exactly analogous to the external report's `lastClaimTime` mapping that must be refreshed on every claim. The pallet's own documentation states the pattern explicitly: "Always start by updating the pool and staker rewards," and both `stake` [3](#0-2)  and `unstake` [4](#0-3)  correctly write the updated `pool_info` back with `Pools::<T>::insert(pool_id, pool_info)`.

`harvest_rewards`, however, computes the same updated `pool_info` via `Self::update_pool_and_staker_rewards` [5](#0-4)  purely to read `pool_info.reward_asset_id`/`pool_info.account` for the transfer, and then returns without ever calling `Pools::<T>::insert`. As a result `Pools::<T>::get(pool_id).last_update_block` and `.reward_per_token_stored` remain at their pre-harvest values in on-chain storage.

`reward_per_token` recomputes accrual as `reward_per_token_stored + reward_rate_per_block * (now - last_update_block) * PRECISION / total_tokens_staked` [6](#0-5) . Because `harvest_rewards` never advances `last_update_block`/`reward_per_token_stored`, the next `stake`/`unstake`/`harvest_rewards` call on the same pool will recompute the accrual over the entire stale interval using whatever `total_tokens_staked` is in effect at that later call, rather than the value(s) that were actually staked during the sub-intervals that straddle the un-persisted harvest. Existing guards (`update_pool_and_staker_rewards` being "side-effect free by design", per the module docs) do not stop this because the pallet's convention that "the top level pallet Call method" is responsible for persisting the computed state is violated specifically in `harvest_rewards`.

### Impact Explanation
If `total_tokens_staked` changes (via `stake`/`unstake` from any staker) between an un-persisted `harvest_rewards` call and the next storage-persisting operation on the pool, the reward-per-token accrued over that stale window is computed against the wrong denominator for part of the interval. This misallocates reward-asset-denominated funds among stakers of the pool — some stakers receive more, others receive less than their rightful share — which is a fund-accounting/value-conservation violation for a pallet that moves real reward assets out of a pool account (`T::Assets::transfer`). It does not require any privileged, malicious, or off-chain actor: any staker calling the permissionless `harvest_rewards` extrinsic triggers it.

### Likelihood Explanation
High. `harvest_rewards` is a publicly callable, unprivileged extrinsic (`ensure_signed`), and any staking activity (`stake`/`unstake` by any account in the same pool) occurring after a harvest and before the pool's next state-persisting call reliably exercises the stale-state path. No race condition or adversarial timing beyond ordinary pool usage is needed.

### Recommendation
Persist the recomputed pool state in `harvest_rewards`, mirroring `stake`/`unstake`:
```rust
let (pool_info, mut staker_info) =
    Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;

// ... transfer ...

Pools::<T>::insert(pool_id, pool_info); // <- add this line
```

### Proof of Concept
1. Pool `P` has stakers A (1000 tokens) and B (1000 tokens); `reward_rate_per_block = R`.
2. At block `N`, A calls `harvest_rewards`. In-memory, `pool_info.reward_per_token_stored` and `last_update_block` are advanced to `N`, and A's `staker_info.rewards` correctly reflects accrual up to `N` — but `Pools::<T>` in storage still shows `last_update_block = N0 < N`.
3. Before any other pool-touching call, B stakes an additional 1000 tokens via `stake` at block `N+1`. Because `stake` reads the stale `Pools::<T>::get(pool_id)` (still at `last_update_block = N0`), it computes `reward_per_token` accrual for the interval `[N0, N+1]` using `total_tokens_staked = 1000` (pre-B's-increase) for the *entire* interval, even though the correct accounting is `[N0, N]` at 1000 staked and `[N, N+1]` should not yet include B's new stake for reward purposes attributable before B staked.
4. This mis-prices the reward-per-token progression: subsequent harvests by A or B use a `reward_per_token_stored` baseline that never reflects the true block at which the last persisted update occurred, cumulatively skewing each staker's share of the fixed `reward_rate_per_block` budget away from their real time-weighted stake, transferring value between stakers incorrectly out of the shared pool reward account.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L523-545)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L562-615)
```rust
		/// Harvest unclaimed pool rewards.
		///
		/// Parameters:
		/// - origin: must be the `staker` if the pool is still active. Otherwise, any account.
		/// - pool_id: the pool to harvest from.
		/// - staker: the account for which to harvest rewards. If `None`, the caller is used.
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
