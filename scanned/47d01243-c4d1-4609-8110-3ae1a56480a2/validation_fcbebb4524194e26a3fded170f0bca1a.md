## Analysis

The exact bug class from the Zivoe report — "reward accounting advances its internal timestamp even during periods with zero stakers, silently discarding the rewards that should have accrued for that idle window, while the underlying tokens remain stuck in the contract" — has a structural analog in `pallet-asset-rewards`.

### Title
Idle-period rewards in `pallet-asset-rewards` are permanently discarded and become unrecoverable once a new staker joins - (`substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` implements a Synthetix-style "reward-per-token" staking rewards pool [1](#0-0) . Like the Zivoe contracts, it special-cases `total_tokens_staked == 0` only in the *rate accrual* calculation, but not in the *timestamp advancement* logic, so any reward notionally due during a period with zero stakers is silently dropped rather than carried forward or refunded.

### Finding Description
`reward_per_token()` intentionally does not accrue `reward_per_token_stored` while `total_tokens_staked` is zero: [2](#0-1) 

However, `update_pool_rewards()` — which is always called together with `reward_per_token()` inside `update_pool_and_staker_rewards()` — unconditionally advances `last_update_block` to the current block regardless of whether any stake existed during the elapsed window: [3](#0-2) 

This is invoked from the public, unprivileged `stake()` extrinsic on every call: [4](#0-3) 

Reward tokens are pre-funded into the pool's dedicated account ahead of time (either via direct transfer or the convenience call `deposit_reward_tokens`), and the pool is configured to emit at a fixed `reward_rate_per_block` until `expiry_block`: [5](#0-4) 

Sequence that strands funds:
1. A pool has stakers; all of them fully `unstake()`, which removes their `PoolStakers` entries once `amount` and `rewards` reach zero, driving `total_tokens_staked` to `0`: [6](#0-5) 
2. Time passes (blocks elapse) while `total_tokens_staked == 0`. The pool's reward account still holds tokens meant to be emitted at `reward_rate_per_block` for this window.
3. Any unprivileged account calls `stake()`. This triggers `update_pool_and_staker_rewards` → `reward_per_token()` returns the *unchanged* stored value (because `total_tokens_staked` was zero going into this call) → `update_pool_rewards()` still sets `last_update_block = now`. The entire idle window's notional reward (`reward_rate_per_block * elapsed_blocks`) is discarded — it is never added to `reward_per_token_stored`, so no current or future staker can ever claim it via `harvest_rewards()`.
4. Because `PoolStakers` is now non-empty (the new staker's entry exists), `cleanup_pool()` — the only mechanism to return unused reward balance to the admin — is permanently blocked by `ensure!(stakers.is_none(), Error::<T>::NonEmptyPool)`: [7](#0-6) 

No other function reconciles this surplus. The pool's reward account simply retains a balance permanently exceeding what `reward_per_token_stored` accounts for, and it can never again be attributed to any staker nor reclaimed by the admin (since reclamation strictly requires zero stakers).

### Impact Explanation
Reward-asset funds transferred into a pool's dedicated account for distribution become permanently unattributable and unrecoverable whenever the pool experiences a zero-stake gap followed by any new stake, mirroring the Zivoe M-6 "funds locked forever" impact. This is a direct, unprivileged loss/lock of pool-held value with no admin or governance action required to trigger — any ordinary staker re-entering the pool causes the loss and simultaneously forecloses the admin's only recovery path (`cleanup_pool`).

### Likelihood Explanation
The trigger requires no privileged role, malicious validator, or off-chain assumption — only ordinary usage: full unstake by all participants (a normal user action, e.g. reacting to low APY or approaching pool expiry) followed by any account calling `stake()` again. Pools nearing `expiry_block`, low-TVL pools, or pools where participants unbond en masse are realistically exposed to this idle-window condition.

### Recommendation
When `total_tokens_staked` is zero, `update_pool_rewards()`/`reward_per_token()` should either (a) not advance `last_update_block` past the point where `total_tokens_staked` last became zero (so the un-emitted rate window is preserved and applied once a staker returns), or (b) treat the idle period as consuming zero reward budget, requiring the elapsed idle blocks to be excluded from `rewardable_blocks_elapsed` rather than allowing `last_update_block` to silently swallow them. Additionally, `cleanup_pool` should be reachable (or an equivalent "sweep unattributed surplus" path should exist) even after stakers re-join, so idle-window surplus is not irrecoverably locked once `PoolStakers` becomes non-empty again.

### Proof of Concept
1. `create_pool` with `reward_rate_per_block = R`, `expiry_block = far future`.
2. Admin/user funds the pool account with `deposit_reward_tokens` for an amount covering `R * N` blocks.
3. Staker A calls `stake(pool_id, X)`.
4. Staker A calls `unstake(pool_id, X, None)` — `total_tokens_staked` becomes `0`, `PoolStakers` entry removed (assuming zero pending `rewards`).
5. Advance the chain by `M` blocks (simulating idle staking period) with no calls to the pool.
6. Any account calls `stake(pool_id, Y)` — `reward_per_token()` returns the previously stored value unchanged (because pre-call `total_tokens_staked == 0`), but `update_pool_rewards` sets `last_update_block` to the current block, discarding `R * M` worth of reward emission.
7. Attempt `cleanup_pool(pool_id)` as admin — fails with `Error::NonEmptyPool` because the new staker's `PoolStakers` entry exists.
8. Result: `R * M` reward tokens remain in the pool account, are never reflected in `reward_per_token_stored`, are never claimable via `harvest_rewards`, and are unreclaimable via `cleanup_pool` — permanently stranded.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L29-41)
```rust
//! Once created, holders of the 'staking asset' can 'stake' them in a corresponding pool, which
//! creates a Freeze on the asset.
//!
//! Once staked, rewards denominated in 'reward asset' begin accumulating to the staker,
//! proportional to their share of the total staked tokens in the pool.
//!
//! Reward assets pending distribution are held in an account unique to each pool.
//!
//! Care should be taken by the pool operator to keep pool accounts adequately funded with the
//! reward asset.
//!
//! The pool admin may increase reward rate per block, increase expiry block, and change admin.
//!
```

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L543-554)
```rust
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
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L667-688)
```rust
		/// Convenience method to deposit reward tokens into a pool.
		///
		/// This method is not strictly necessary (tokens could be transferred directly to the
		/// pool pot address), but is provided for convenience so manual derivation of the
		/// account id is not required.
		#[pallet::call_index(7)]
		pub fn deposit_reward_tokens(
			origin: OriginFor<T>,
			pool_id: PoolId,
			amount: T::Balance,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&caller,
				&pool_info.account,
				amount,
				Preservation::Preserve,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L697-705)
```rust
		pub fn cleanup_pool(origin: OriginFor<T>, pool_id: PoolId) -> DispatchResult {
			let who = ensure_signed(origin)?;

			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			ensure!(pool_info.admin == who, BadOrigin);

			let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
			ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);

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
