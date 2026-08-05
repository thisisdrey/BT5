### Title
Reward-per-block accrued while `total_tokens_staked == 0` is silently dropped and can be permanently swept by the pool admin - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
The `pallet-asset-rewards` pool implements the same Synthetix-style "reward-per-token accumulator" pattern as the vulnerable `BaseRewardPool`/`VE3DRewardPool` contracts in the external report. When no tokens are staked, `reward_per_token()` skips accrual but the caller still advances `last_update_block`, so the reward-rate-driven emission that "should" have accrued during the zero-stake window is never credited to any staker and is effectively lost from the accounting model, while the underlying asset balance sitting in the pool's pot account remains intact and reachable by the admin.

### Finding Description
`reward_per_token` returns the *stored* value unchanged whenever `total_tokens_staked` is zero, without accounting for the elapsed blocks: [1](#0-0) 

However, every call site that uses this value — `update_pool_and_staker_rewards` / `update_pool_rewards` — unconditionally advances `last_update_block` to the current block after calling `reward_per_token()`: [2](#0-1) 

This exactly mirrors the reported bug class: the emission model (`reward_rate_per_block × blocks_elapsed`) implicitly assumes continuous accrual, but any block range in which `total_tokens_staked == 0` is silently excised from the reward math the moment the next `update_pool_and_staker_rewards` call runs (e.g., on the first `stake`), because it moves `last_update_block` forward without folding the skipped interval into `reward_per_token_stored`. Unlike the original bug, the pallet exposes `deposit_reward_tokens`, a permissionless entrypoint that lets *any* signed account fund a pool's reward pot for later distribution to stakers: [3](#0-2) 

And `cleanup_pool`, callable only by the pool admin, sweeps the *entire remaining pot balance* back to the admin as long as there are currently no stakers: [4](#0-3) 

Because a pool can exist with `reward_rate_per_block > 0` and zero stakers for an arbitrary number of blocks (before the first `stake` call, or after all stakers fully unstake), any tokens deposited via `deposit_reward_tokens` by a third party during that window are never earmarked to a staker in `reward_per_token_stored`. If the admin calls `cleanup_pool` while `total_tokens_staked` is still zero, it takes the entire pot — including funds a third party deposited intending them to be distributed as staking rewards — with no guard checking whether deposited rewards were ever actually attributed to anyone.

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary" invariant for reward payouts. A pool admin (who need not be a chain-level privileged actor — pool creation/admin roles are assigned by a configurable, potentially permissionless origin per pallet docs) can create a pool, wait for/allow third parties to fund it via `deposit_reward_tokens`, and then reclaim those funds via `cleanup_pool` before anyone stakes and claims — the honest reward accounting never records those funds as "belonging" to a staker share, so nothing prevents the sweep. Even absent the admin action, the accrual defect alone means reward-asset value corresponding to zero-supply periods is unrecoverable by legitimate stakers, i.e., silently misallocated rather than distributed as intended by the pallet's stated JIT rewards algorithm.

### Likelihood Explanation
Any pool with a nonzero `reward_rate_per_block` naturally has a startup window between `create_pool` and the first `stake` call where `total_tokens_staked == 0`; this requires no attacker collusion, malicious relayer, or governance abuse — it is a routine operational sequence (create pool, fund it, wait for stakers) explicitly acknowledged by the pallet's own doc comment warning operators to "keep pool accounts adequately funded," which does not address this specific accrual/sweep interaction.

### Recommendation
When `total_tokens_staked` is zero, either (a) refuse to advance `last_update_block` so unrealized reward-rate emission is deferred until stake exists, or (b) explicitly track/refund undistributed reward-rate emission that occurred during zero-supply windows. Additionally, `cleanup_pool` should account for and exclude/return-to-original-depositor any reward-asset balance that was deposited via `deposit_reward_tokens` but never attributed through `reward_per_token_stored`, rather than transferring the full pot to the admin whenever `PoolStakers` is momentarily empty.

### Proof of Concept
1. Admin calls `create_pool` with `reward_rate_per_block = R > 0`; `total_tokens_staked = 0`, `last_update_block = 0`. [5](#0-4) 
2. Some blocks pass (`N` blocks) with no stakers. A third party calls `deposit_reward_tokens` to fund the pool pot, expecting future stakers to earn it. [3](#0-2) 
3. Before anyone calls `stake`, the admin calls `cleanup_pool`. Since `PoolStakers::<T>::iter_key_prefix(pool_id).next()` is `None` (no stakers ever joined), the check passes and the entire pot balance (including the third-party deposit) is transferred to the admin, and the pool is removed. [6](#0-5) 
4. Even if a staker joins first: the moment `stake` triggers `update_pool_and_staker_rewards` → `reward_per_token()` → `update_pool_rewards`, the `N` blocks with zero stake are folded into `last_update_block` advancement with no compensating increase to `reward_per_token_stored`, permanently excluding `R × N` reward-asset units worth of emission from ever being claimable by any staker, while those funds may remain in the pot only recoverable by the admin via `cleanup_pool` once the pool empties again.

### Citations

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L787-792)
```rust
		pub(super) fn reward_per_token(
			pool_info: &PoolInfoFor<T>,
		) -> Result<T::Balance, DispatchError> {
			if pool_info.total_tokens_staked.is_zero() {
				return Ok(pool_info.reward_per_token_stored);
			}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L870-884)
```rust
		// Create the pool.
		let pool = PoolInfoFor::<T> {
			staked_asset_id: staked_asset_id.clone(),
			reward_asset_id: reward_asset_id.clone(),
			reward_rate_per_block,
			total_tokens_staked: 0u32.into(),
			reward_per_token_stored: 0u32.into(),
			last_update_block: 0u32.into(),
			expiry_block,
			admin: admin.clone(),
			account: Self::pool_account_id(&pool_id),
		};

		// Insert it into storage.
		Pools::<T>::insert(pool_id, pool);
```
