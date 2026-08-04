### Title
Reward tokens accrued while `total_tokens_staked` is zero are permanently lost with no rollover mechanism - ([File: substrate/frame/asset-rewards/src/lib.rs])

### Summary
`pallet-asset-rewards` (`substrate/frame/asset-rewards`) explicitly implements the same reward algorithm as the reported Synthetix `StakingRewards.sol` contract (the pallet's own docs cite it directly, `substrate/frame/asset-rewards/src/lib.rs:65-79`). It inherits the same broken invariant: reward-rate emission accrues purely as a function of elapsed blocks, and any reward emitted while the pool has zero stakers is silently dropped rather than rolled forward.

### Finding Description
`reward_per_token` computes accrued rewards per staked token strictly from `reward_rate_per_block * blocks_elapsed / total_tokens_staked`. When `total_tokens_staked.is_zero()`, the function short-circuits and returns the previously stored value unchanged, without advancing `last_update_block`... except `last_update_block` IS advanced separately in `update_pool_rewards`, which is called unconditionally right after: [1](#0-0) 

```
pub(super) fn reward_per_token(...) -> ... {
    if pool_info.total_tokens_staked.is_zero() {
        return Ok(pool_info.reward_per_token_stored);
    }
    ...
}
``` [2](#0-1) 

```
pub fn update_pool_rewards(...) -> ... {
    let mut new_pool_info = pool_info.clone();
    new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number();
    new_pool_info.reward_per_token_stored = reward_per_token;
    Ok(new_pool_info)
}
```

Because `update_pool_and_staker_rewards` always calls `update_pool_rewards` and unconditionally moves `last_update_block` forward to the current block regardless of whether `total_tokens_staked` was zero, the blocks that elapsed with zero stake are permanently excised from future reward accounting: `reward_per_token` for that window is never computed against a non-zero denominator, and the emission "budget" implied by `reward_rate_per_block` for that empty window (analogous to the reported `rewardRate * (block.timestamp - startTime)`) is never redirected to a "pending"/unused pool for later attribution. The reward asset that would have been distributed for that period is not credited to anyone, and admin can only recover it via `cleanup_pool` (which requires zero stakers and full pool teardown) rather than a "notify new rewards" style top-up that reuses stranded funds, exactly mirroring the disputed-but-accepted (as "protocol leaked value") pattern in the source report.

This directly parallels the two functions the pool admin calls that increase/extend emission — `create_pool` and `set_pool_reward_rate_per_block` — which set `reward_rate_per_block` without any accounting for windows where `total_tokens_staked` is zero: [3](#0-2) 

The `stake` call, which is the first deposit analog to Y2K's "first deposit" trigger, calls `update_pool_and_staker_rewards` *before* increasing `total_tokens_staked`, so any elapsed blocks since `create_pool` (or since the last staker fully unstaked) with zero stake have their reward-rate emission dropped by the `is_zero()` branch above without being carried forward: [4](#0-3) 

### Impact Explanation
This is a public, unprivileged-triggerable value-leak: any period with `total_tokens_staked == 0` (right after `create_pool`, or after all stakers fully `unstake`) permanently strands `reward_rate_per_block * blocks_elapsed` worth of reward asset in the pool account with no attribution path to any staker and no automatic rollover into subsequent emission. This matches the "Impact Gate" criterion of public underpriced/lost value affecting reward payout correctness for a live pallet in `paritytech/polkadot-sdk`, without requiring a malicious peer, validator, or admin — it happens under entirely normal usage patterns (e.g., a pool created before its first staker joins, or a pool that becomes fully unstaked before new stakers arrive).

### Likelihood Explanation
High likelihood: this occurs under ordinary operational conditions (gap between `create_pool` and first `stake`, or between last `unstake` and next `stake`) with no attacker action required, only that pool activity naturally has such a gap — the exact scenario described in the referenced report.

### Recommendation
Track unattributed reward emission explicitly: when `update_pool_rewards`/`reward_per_token` detects `total_tokens_staked.is_zero()`, do not silently discard the blocks elapsed; either (a) do not advance `last_update_block` past the last non-zero-supply block until a staker rejoins, so the accrual window naturally deducts, or (b) accumulate the un-attributable reward budget into a separate "unallocated rewards" counter that can be redistributed or reclaimed by admin via `set_pool_reward_rate_per_block`/`deposit_reward_tokens` bookkeeping, consistent with the "next notifyRewardAmount" mitigation recommended in the original report.

### Proof of Concept
1. Admin calls `create_pool` with `reward_rate_per_block = 10` reward tokens/block and pool starts at block `N`. [5](#0-4) 
2. No staker calls `stake` for 10 blocks; `total_tokens_staked` remains `0`.
3. At block `N+10`, a staker calls `stake`. `update_pool_and_staker_rewards` → `reward_per_token` is invoked; since `total_tokens_staked.is_zero()` at call time, it returns `reward_per_token_stored` unchanged (no accrual computed for blocks `N..N+10`). [6](#0-5) 
4. `update_pool_rewards` still advances `last_update_block` to `N+10`. [7](#0-6) 
5. From this point on, the reward window `N..N+10` (worth `10 * 10 = 100` reward tokens per the pool's emission rate) is permanently excluded from all future `reward_per_token` calculations — no staker will ever be credited for it, and it is not folded into a future `notifyRewardAmount`-equivalent action.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L449-467)
```rust
		pub fn create_pool(
			origin: OriginFor<T>,
			staked_asset_id: Box<T::AssetId>,
			reward_asset_id: Box<T::AssetId>,
			reward_rate_per_block: T::Balance,
			expiry: DispatchTime<BlockNumberFor<T>>,
			admin: Option<T::AccountId>,
		) -> DispatchResult {
			let creator = T::CreatePoolOrigin::ensure_origin(origin)?;
			<Self as RewardsPool<_>>::create_pool(
				&creator,
				*staked_asset_id,
				*reward_asset_id,
				reward_rate_per_block,
				expiry,
				&admin.unwrap_or_else(|| creator.clone()),
			)?;
			Ok(())
		}
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L622-635)
```rust
		#[pallet::call_index(4)]
		pub fn set_pool_reward_rate_per_block(
			origin: OriginFor<T>,
			pool_id: PoolId,
			new_reward_rate_per_block: T::Balance,
		) -> DispatchResult {
			let caller = T::CreatePoolOrigin::ensure_origin(origin.clone())
				.or_else(|_| ensure_signed(origin))?;
			<Self as RewardsPool<_>>::set_pool_reward_rate_per_block(
				&caller,
				pool_id,
				new_reward_rate_per_block,
			)
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
