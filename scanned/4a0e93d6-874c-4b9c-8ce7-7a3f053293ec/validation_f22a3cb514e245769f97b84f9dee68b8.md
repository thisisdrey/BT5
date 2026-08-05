## Title
Reward-per-token accumulator in `pallet-asset-rewards` can be permanently deflated via floor-division rounding while `last_update_block` still advances - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards`'s `reward_per_token` computes a per-block reward accrual using integer division scaled only by a small `PRECISION_SCALING_FACTOR` (`4096`, i.e. `2^12`) instead of a large scaling factor (e.g. `1e18`) as in the original Yield/Notional contract that the H-04 report describes. `update_pool_rewards` unconditionally advances `last_update_block` to the current block every time it is called, even when the computed `reward_per_token` delta rounds down to zero. This is the exact bug class from the external report: the accumulator can be permanently starved by repeatedly forcing the elapsed-time window to be tiny (1 block), while the "last updated" marker keeps moving forward, erasing reward-accrual time forever.

### Finding Description
`reward_per_token` in `substrate/frame/asset-rewards/src/lib.rs`: [1](#0-0) 

computes:
```
reward_per_token_stored += reward_rate_per_block * rewardable_blocks_elapsed * 4096 / total_tokens_staked
```
Any call that touches a pool — `stake`, `unstake`, or `harvest_rewards` — invokes `update_pool_and_staker_rewards`, which calls `reward_per_token` and then `update_pool_rewards`: [2](#0-1) 

`update_pool_rewards` always sets `new_pool_info.last_update_block = current_block_number()`, regardless of whether `reward_per_token` actually changed. If `reward_rate_per_block * rewardable_blocks_elapsed * 4096 < total_tokens_staked`, the division truncates to `0`, so `reward_per_token_stored` stays exactly the same, but `last_update_block` is bumped forward. The next call computes elapsed blocks from this new, later `last_update_block`, so the skipped interval's rewards are permanently lost — not merely delayed, since the numerator only ever depends on `rewardable_blocks_elapsed` since the *last* update, not the total elapsed time since genesis.

An unprivileged staker (or even non-staker calling on their own account) can trigger this every block cheaply via `stake` with a small/zero amount or `unstake`/`harvest_rewards`: [3](#0-2) [4](#0-3) 

Because `rewardable_blocks_elapsed` is reset to 1 block each time, the effective precision loss is far worse than the original report's 1e18 scaling scenario: here the scaling factor is only `4096`, so any pool with a `total_tokens_staked` roughly `4096×` larger than `reward_rate_per_block` (a very ordinary configuration for a pool with many stakers or a high-decimal asset) suffers from this every single block, with or without an attacker.

### Impact Explanation
This directly matches "public underpriced work that degrades block production or stalls bridge processing" / reward-payout state that fails to conserve value: legitimate stakers permanently lose rewards that the pool admin funded, because the reward accumulator is silently starved while `last_update_block` marches forward, closing the window over which that reward should have accrued. Anyone (staker or third party specifying `staker: Some(..)` for `unstake`/`harvest_rewards` after pool expiry, or the staker themselves at any time) can cheaply keep calling these entry points once per block to guarantee `rewardable_blocks_elapsed == 1` forever, making the rounding-to-zero condition persistent for the whole pool duration rather than an occasional edge case.

### Likelihood Explanation
No malicious peer, validator, collator, governance, or leaked key is required — this is exploitable (or even happens naturally from routine `stake`/`unstake`/`harvest_rewards` activity) by any signed account interacting with an existing pool. The only requirement is `reward_rate_per_block * 4096 < total_tokens_staked`, which is a realistic configuration (e.g. low emission rate relative to a large staked-asset supply/decimals), making this straightforward to hit both accidentally and adversarially.

### Recommendation
Do not advance `last_update_block` when the computed `reward_per_token` delta is zero (mirroring the report's suggested fix), or increase `PRECISION_SCALING_FACTOR` substantially (e.g. to `1e18`-scale) and/or accumulate un-applied remainder instead of discarding it, so time-elapsed since the pool's true last non-zero update is never lost to floor-division.

### Proof of Concept
1. Admin creates a pool with `reward_rate_per_block = R` and stakers deposit `total_tokens_staked = S` such that `R * 4096 < S` (e.g. `R = 100`, `S = 1_000_000`).
2. Attacker (or any staker) calls `stake(pool_id, 0)` (or `unstake`/`harvest_rewards`) once per block.
3. Each call triggers `reward_per_token`: `100 * 1 * 4096 / 1_000_000 = 0` (floor), so `reward_per_token_stored` never increases.
4. Each call also sets `last_update_block = current_block`, via `update_pool_rewards` at [2](#0-1) , so the next computation starts from `rewardable_blocks_elapsed = 1` again.
5. Result: for the entire pool lifetime, `reward_per_token_stored` stays `0` and no staker ever accrues rewards from `derive_rewards` at [5](#0-4) , even though the admin funded the pool with reward tokens expecting them to be distributed.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L815-824)
```rust
		fn derive_rewards(
			staker_info: &PoolStakerInfo<T::Balance>,
			reward_per_token: &T::Balance,
		) -> Result<T::Balance, DispatchError> {
			Ok(staker_info
				.amount
				.ensure_mul(reward_per_token.ensure_sub(staker_info.reward_per_token_paid)?)?
				.ensure_div(PRECISION_SCALING_FACTOR.into())?
				.ensure_add(staker_info.rewards)?)
		}
```
