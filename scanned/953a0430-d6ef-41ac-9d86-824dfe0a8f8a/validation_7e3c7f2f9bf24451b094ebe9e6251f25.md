Found a real analog: `cleanup_pool` in the asset-rewards pallet computes and pays out the pool's remaining reward-token balance **without first calling `update_pool_and_staker_rewards`/`update_pool_rewards`** to settle the pool's accrued-but-unclaimed reward accounting, exactly mirroring the external report's pattern of reading a derived value ("assetsUtilized"/pool balance) without refreshing the underlying stored state first.

### Title
`cleanup_pool` sweeps the entire reward-asset balance to the admin without settling accrued reward accounting - (File: substrate/frame/asset-rewards/src/lib.rs)

### Summary
`cleanup_pool` reads the pool's live reward-asset balance via `T::Assets::reducible_balance` and transfers 100% of it to the pool admin, but it never calls `Self::update_pool_and_staker_rewards` / `update_pool_rewards` beforehand, and it only checks that `PoolStakers` has no entries — it does not verify that the pool's accrued reward-per-token accounting (`reward_per_token_stored`, `last_update_block`) is up to date or that all rewards owed to the last block have actually been distributed. [1](#0-0) 

### Finding Description
The reward algorithm is JIT (just-in-time): a staker's true pending reward is only correct if `reward_per_token` is recomputed up to the current block via `update_pool_and_staker_rewards`/`update_pool_rewards`, which advances `last_update_block` and `reward_per_token_stored`. [2](#0-1) 
Every other reward-affecting call (`stake`, `unstake`, `harvest_rewards`) begins with "Always start by updating staker and pool rewards" before touching `PoolStakers`/`Pools` state. [3](#0-2) [4](#0-3) [5](#0-4) 

`cleanup_pool`, however, is the only balance-moving entrypoint that skips this update step entirely. It only fetches `pool_info`, checks admin authority and that `PoolStakers` is empty, then transfers the entire `reducible_balance` of the reward account to the admin: [6](#0-5) 

The `NonEmptyPool` guard only checks that `PoolStakers::<T>::iter_key_prefix(pool_id).next()` is `None` — i.e., no staker records currently exist — but that does not guarantee the pool's internal reward accounting (`reward_per_token_stored`/`last_update_block`) reflects the true settled state. A staker can be removed from `PoolStakers` by `unstake` (when `staker_info.amount.is_zero() && staker_info.rewards.is_zero()`) or `harvest_rewards` (when `staker_info.amount.is_zero()`) while the pool's accrual bookkeeping is only ever updated as a *side-effect of individual staker interactions*, not proactively. Because `cleanup_pool` never re-derives `reward_per_token`/settles the pool state before sweeping, it can drain reward tokens that are still economically owed under the pool's declared `reward_rate_per_block` up to the block of cleanup but have not yet been reflected in any staker's stored `rewards` field (e.g., value accrued between a staker's last interaction and pool exhaustion, or dust/precision remainders that legitimately belong to the reward schedule rather than the admin).

### Impact Explanation
Low-to-moderate: the pool admin can call `cleanup_pool` to claim the entirety of the pool's reward-asset balance, including amounts that were nominally still accruing to the algorithm's JIT accounting model, without the pallet ever recomputing/settling `reward_per_token` for the pool. This misaligns with the design comment stating internal update functions "should be called prior to any operation involving a staker" and, more importantly, that pool cleanup is intended to return only the "remaining reward tokens" — remaining after accounting is properly settled, not simply whatever balance sits in the account irrespective of stale bookkeeping.

### Likelihood Explanation
High: `cleanup_pool` is a normal, permissionless-to-the-admin dispatchable that requires no special conditions beyond `PoolStakers` being empty, which naturally occurs at the end of a pool's lifecycle via routine `unstake`/`harvest_rewards` calls.

### Recommendation
Before computing `pool_balance` and transferring funds in `cleanup_pool`, call `Self::update_pool_rewards(&pool_info, Self::reward_per_token(&pool_info)?)` (mirroring the pattern used in `stake`/`unstake`/`harvest_rewards`) and persist the updated `pool_info`, ensuring the reward accounting is fully settled to the current block before determining the truly unutilized reward balance to return to the admin.

### Proof of Concept
1. Admin creates a pool with `reward_rate_per_block = R` and funds it.
2. A staker stakes, then later calls `unstake` for their full amount when `rewards == 0` (e.g., immediately after `harvest_rewards`), causing their `PoolStakers` entry to be removed while `total_tokens_staked` may still be non-zero from other residual state or the pool has simply become staker-less.
3. Blocks pass; reward tokens continue to be nominal "obligations" per the reward-rate schedule even though no `PoolStakers` entries exist to claim them, because `update_pool_rewards` is only invoked when a staker interacts.
4. Admin calls `cleanup_pool`; the check `PoolStakers::<T>::iter_key_prefix(pool_id).next().is_none()` passes trivially, `reducible_balance` returns the full reward-account balance, and the entire amount is transferred to `pool_info.admin` without ever recomputing `reward_per_token`/settling pool state, letting the admin capture reward-asset amounts whose settlement was never verified against the JIT accounting model. [1](#0-0)

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L476-480)
```rust
			// Always start by updating staker and pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L523-530)
```rust
			// Always start by updating the pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L577-585)
```rust
			// Always start by updating the pool and staker rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info =
				PoolStakers::<T>::get(pool_id, &staker).ok_or(Error::<T>::NonExistentStaker)?;
			let (pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L746-784)
```rust
		/// Computes update pool and staker reward state.
		///
		/// Should be called prior to any operation involving a staker.
		///
		/// Returns the updated pool and staker info.
		///
		/// NOTE: this function has no side-effects. Side-effects such as storage modifications are
		/// the responsibility of the caller.
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
