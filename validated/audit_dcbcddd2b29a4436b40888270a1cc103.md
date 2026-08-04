### Title
Permissionless `deposit_reward_tokens` funds can be fully swept by the pool admin via `cleanup_pool` before any staker exists - (`substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` exposes a permissionless `deposit_reward_tokens` extrinsic that lets *any* signed account push reward-asset tokens into a pool's pot account, with no requirement that a staker exist and no tracking of who contributed what. If the pool has zero stakers (either because no one has staked yet, or all stakers have exited), the admin can call `cleanup_pool`, which sweeps 100% of the pot balance — including funds deposited by unrelated third parties — back to the admin alone.

### Finding Description
`deposit_reward_tokens` performs a bare asset transfer from the caller into the pool's account with no state bookkeeping of the depositor or amount contributed relative to other depositors: [1](#0-0) 

This mirrors the reported gauge bug: rewards can be pushed into the pot before any depositor exists. In this pallet, when `total_tokens_staked` is zero, `reward_per_token` deliberately does not advance the accrual counter, so no staker is ever credited for reward accrued during a zero-stake window: [2](#0-1) 

Because those tokens are never credited to any staker's `PoolStakerInfo`, they remain as a raw balance sitting in the pool's pot account (`pool_info.account`). If the pool currently has (or later returns to) zero stakers, the admin-only `cleanup_pool` extrinsic sweeps the *entire* remaining pot balance to the admin, with no distinction between admin-funded reward tokens and third-party donations made via `deposit_reward_tokens`: [3](#0-2) 

`cleanup_pool` only checks that no `PoolStakers` entries exist for the pool — it does not check who deposited the reward-asset balance: [4](#0-3) 

The corrupted/mis-settled value is the reward-asset balance held at `pool_info.account` (`T::Assets::transfer(... &pool_info.account, &pool_info.admin, pool_balance ...)`), which is fully redirected to `pool_info.admin` regardless of its provenance. There is no existing guard preventing `deposit_reward_tokens` from succeeding when `total_tokens_staked == 0`, and no guard in `cleanup_pool` that restricts the swept amount to admin-contributed funds only.

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary" invariant for reward payouts: an unprivileged, permissionless depositor's funds can end up entirely captured by the pool admin instead of the intended stakers, with no attacker needing elevated privileges to trigger the initial loss condition (only a legitimate, non-malicious admin performing a normal, designed operation completes the diversion). Depending on amounts deposited, this can represent a real, unrecoverable loss of value for the depositor.

### Likelihood Explanation
Likelihood is moderate: `deposit_reward_tokens` is unauthenticated beyond `ensure_signed`, so anyone can trigger the vulnerable precondition (deposit while pool has zero stakers) at any time, including immediately after `create_pool`. The only remaining step—`cleanup_pool`—is gated to the admin, but it is a normal, documented pool-lifecycle operation (reclaiming "unutilized reward tokens" per the pallet's own doc comment), not an abuse of governance/admin privilege; it can occur naturally whenever a pool empties out (e.g., all stakers unstake) after third-party deposits were made.

### Recommendation
- Reject `deposit_reward_tokens` (or track contributions separately) when `PoolInfo::total_tokens_staked == 0`, mirroring the original report's fix of reverting deposits when there is no active supply/depositor.
- Alternatively, track deposited-but-unaccrued reward balances separately from admin-owned pool funds, and only allow `cleanup_pool` to reclaim the admin's own residual balance, refunding or preventing capture of externally-deposited, unaccrued rewards.

### Proof of Concept
1. Admin calls `create_pool` for pool id `0` with `staked_asset_id`/`reward_asset_id`, `reward_rate_per_block`, and an `expiry_block` [5](#0-4) .
2. Before any account calls `stake`, an arbitrary unprivileged user `U` calls `deposit_reward_tokens(pool_id=0, amount=X)`, transferring `X` reward-asset tokens into `pool_info.account` [1](#0-0) .
3. Since `PoolStakers::iter_key_prefix(0)` is empty, admin calls `cleanup_pool(pool_id=0)`. `pool_balance` (including `U`'s `X`) is computed via `reducible_balance` and transferred entirely to `pool_info.admin` [6](#0-5) .
4. `U`'s deposited `X` tokens are now held by the admin instead of any staker; `U` has no path to reclaim them, and no staker was ever credited with the reward.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L448-467)
```rust
		#[pallet::call_index(0)]
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L672-688)
```rust
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
