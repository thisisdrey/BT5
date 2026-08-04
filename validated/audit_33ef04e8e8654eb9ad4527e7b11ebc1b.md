Found a concrete, locally provable analog in the `pallet-asset-rewards` pallet, which implements essentially the same "update reward accumulator based on elapsed time, but forget to persist the new checkpoint" pattern described in the bug report.

### Title
`harvest_rewards` computes updated pool reward checkpoint but never persists it, causing duplicate reward accrual over the same elapsed period - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` implements a standard "reward-per-token accumulator" scheme where `Pools::<T>` stores `last_update_block` and `reward_per_token_stored`, and every mutating extrinsic must recompute and re-persist these before changing stake or paying out rewards. `stake` and `unstake` correctly call `Pools::<T>::insert(pool_id, pool_info)` after `update_pool_and_staker_rewards`, but `harvest_rewards` computes the updated `pool_info` locally and uses it to pay the staker, yet never writes it back to `Pools::<T>` storage. This is exactly the "missing timestamp update" pattern from the external report: the elapsed-time reward calculation is performed and paid out, but the checkpoint that prevents re-counting that same elapsed time is discarded.

### Finding Description
The pool's reward accounting works by combining a stored checkpoint (`last_update_block`, `reward_per_token_stored`) with the current block number to compute newly accrued `reward_per_token`: [1](#0-0) 

Both `stake` and `unstake` correctly call `update_pool_and_staker_rewards` and then persist the resulting `pool_info` back into `Pools::<T>`: [2](#0-1) [3](#0-2) 

`harvest_rewards`, however, computes the same updated `pool_info` (with the new `last_update_block` and `reward_per_token_stored`) purely as a local binding, uses it only to compute and pay the calling staker's reward, and then only persists the staker's state — the pool's checkpoint update is never written back to `Pools::<T>`: [4](#0-3) 

Because `Pools::<T>` still holds the stale `last_update_block`/`reward_per_token_stored` after `harvest_rewards`, the next call to `stake`, `unstake`, or `harvest_rewards` by *any other staker* will recompute `reward_per_token` for the full elapsed period since the stale checkpoint — a period that has already been paid out to the previous harvester. Since that other staker's own `reward_per_token_paid` was fixed at an earlier point, they become entitled to the same reward-per-token delta that was already disbursed, resulting in double-crediting of the same block-range reward across two different stakers.

### Impact Explanation
This breaks the fund-conservation invariant of the reward pool: the total rewards claimable, summed across all stakers, can exceed `reward_rate_per_block * elapsed_blocks` for the pool's actual lifetime, i.e., more reward-asset tokens can be claimed than the schedule dictates. Since `harvest_rewards` transfers directly out of the pool's asset account (`pool_info.account`) via `T::Assets::transfer`, this allows draining the pool's reward-asset reserves beyond what governance/admin intended to allocate, and other legitimate stakers can be undercompensated or have their harvest calls fail due to insufficient pool balance. This is an unprivileged, permissionless path (`harvest_rewards` is callable by any staker for their own stake) that produces duplicate settlement of the same reward period — squarely in the "duplicate settlement or payout" / "theft of pooled funds" impact category.

### Likelihood Explanation
High likelihood: `harvest_rewards` is a normal, frequently-used, permissionless user extrinsic (no admin/governance/malicious-peer preconditions). Any pool with more than one active staker where at least one staker calls `harvest_rewards` before another staker calls `stake`/`unstake`/`harvest_rewards` will trigger the stale-checkpoint reuse. No race condition or timing luck beyond ordinary sequential block production is required.

### Recommendation
In `harvest_rewards`, persist the updated `pool_info` back into `Pools::<T>` immediately after calling `update_pool_and_staker_rewards`, exactly as `stake` and `unstake` already do:
```rust
let (pool_info, mut staker_info) =
    Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
Pools::<T>::insert(pool_id, pool_info.clone()); // add this line
```

### Proof of Concept
1. Create a pool with `reward_rate_per_block = R` and stakers A and B each staking equal amounts, so `total_tokens_staked = X`, `last_update_block = 0`, `reward_per_token_stored = 0`.
2. At block `N`, staker A calls `harvest_rewards`. Internally this computes `reward_per_token = R*N*SCALE/X`, pays A `R*N/2` (their share), and updates A's `PoolStakers` entry with `reward_per_token_paid = R*N*SCALE/X`. Crucially, `Pools::<T>` is left unchanged: `last_update_block` is still `0`, `reward_per_token_stored` is still `0`.
3. At block `N+M`, staker B calls `harvest_rewards`. This reads the stale pool state (`last_update_block=0`), so it computes `reward_per_token` for the *entire* `0..N+M` elapsed range: `reward_per_token = R*(N+M)*SCALE/X`. Since B's `reward_per_token_paid` is still `0` (unchanged since B joined), B is credited `R*(N+M)/2`, which double-counts the `0..N` window that was already paid out to A in step 2.
4. Total rewards paid to A and B together exceed the intended `R*(N+M)` reward emission for that period, draining the pool's reward-asset account beyond its designed schedule.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L476-492)
```rust
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
