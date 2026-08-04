## Analysis Result



### Title
Permissionless `deposit_reward_tokens` allows reward-asset donations to be stranded/misdirected when a pool has zero stakers - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards::deposit_reward_tokens` is a public, unprivileged extrinsic that lets anyone transfer reward-asset tokens into a pool's reward account. [1](#0-0)  It performs no check on `pool_info.total_tokens_staked`, so a donation made while a pool has no stakers is not distributed to anyone and can only ever be recovered by the pool `admin` (not the depositor, not future stakers) via `cleanup_pool`.

### Finding Description
Reward accrual is computed by `reward_per_token`, which is the sole mechanism translating elapsed blocks into a per-token reward figure used later by `derive_rewards` to credit individual stakers:

```rust
pub(super) fn reward_per_token(
    pool_info: &PoolInfoFor<T>,
) -> Result<T::Balance, DispatchError> {
    if pool_info.total_tokens_staked.is_zero() {
        return Ok(pool_info.reward_per_token_stored);
    }
    ...
}
``` [2](#0-1) 

When `total_tokens_staked == 0`, the function returns the stored value unchanged — there is no denominator to divide reward-rate emissions across, so no per-staker credit is generated for that window. Meanwhile `update_pool_rewards` still advances `last_update_block` to "now" whenever it's invoked, permanently closing out that period without having credited anyone. [3](#0-2) 

`deposit_reward_tokens` is the public entrypoint that lets an arbitrary signed account push reward-asset balance into the pool's account, with zero validation that the pool currently has stakers able to receive a pro‑rata share:

```rust
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
``` [1](#0-0) 

Because reward emission is entirely rate/time-based (`reward_rate_per_block * elapsed_blocks / total_tokens_staked`) and decoupled from the pool account's actual balance, any deposit made while `total_tokens_staked == 0` is not reflected in `reward_per_token_stored` for that period and is not distributed to stakers who later join and earn strictly according to the rate formula. The only path back to any account is `cleanup_pool`, which requires the caller to be the pool `admin` and requires zero current stakers, and it returns the *entire* remaining reward-asset balance to the admin — not the original depositor and not any staker:

```rust
pub fn cleanup_pool(origin: OriginFor<T>, pool_id: PoolId) -> DispatchResult {
    let who = ensure_signed(origin)?;
    let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
    ensure!(pool_info.admin == who, BadOrigin);
    let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
    ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);
    ...
    T::Assets::transfer(pool_info.reward_asset_id, &pool_info.account, &pool_info.admin, pool_balance, Preservation::Expendable)?;
``` [4](#0-3) 

This mirrors the external report's broken invariant exactly: a public "donate to pot" wrapper (`notifyRewardAmount` ↔ `deposit_reward_tokens`) credits a reward pool without checking `globalTotalVotes > 0` ↔ `total_tokens_staked > 0`, so value is not settled to the intended pro-rata beneficiaries and instead is only recoverable by a privileged party (admin), violating the conservation/settlement pivot that value must "settle exactly once to the rightful beneficiary and amount."

### Impact Explanation
Any unprivileged user who calls `deposit_reward_tokens` on a pool that currently has zero stakers (e.g., a freshly created pool, or one where all stakers have unstaked) has their reward-asset donation permanently excluded from the pro-rata distribution formula for that time window. Funds are not lost from total issuance, but they are misdirected: the sole path out is `cleanup_pool`, gated to the pool `admin`, transferring the entire remaining balance to the admin instead of back to the depositor or to the eventual stakers who should be earning it. This is a fund-misdirection / stuck-fund condition reachable by any unprivileged account with no admin or governance involvement required to trigger it.

### Likelihood Explanation
Likelihood is medium: `deposit_reward_tokens` is fully permissionless and requires no special timing knowledge beyond observing `Pools::<T>::get(pool_id).total_tokens_staked == 0`, which is public on-chain state, readable via storage before submitting the deposit extrinsic. Newly created pools naturally start with zero stakers, and pools can transiently empty when all stakers unstake, giving frequent windows where the flaw is exploitable/triggerable by accident or intentionally.

### Recommendation
In `deposit_reward_tokens`, reject deposits when `pool_info.total_tokens_staked.is_zero()`, or alternatively update `reward_per_token`/pool accounting to carry forward undistributed reward-rate emissions (and deposited amounts) instead of silently advancing `last_update_block` without any accrual when there are no stakers. Additionally, consider allowing the original depositor (not just the admin) to reclaim funds deposited during a zero-staker window, to avoid misdirecting third-party donations to the admin via `cleanup_pool`.

### Proof of Concept
1. Admin creates a pool via `create_pool` with `reward_rate_per_block > 0`; `total_tokens_staked == 0` at genesis. [5](#0-4) 
2. Any unprivileged account calls `deposit_reward_tokens(pool_id, amount)`, transferring reward-asset tokens into the pool's reward account. [6](#0-5) 
3. Time passes with `total_tokens_staked` still zero. Any call that triggers `update_pool_and_staker_rewards`/`reward_per_token` during this window returns `reward_per_token_stored` unchanged (no accrual) while advancing `last_update_block`. [2](#0-1) 
4. A staker later calls `stake`, joining the pool; going forward, rewards accrue strictly per `reward_rate_per_block`/`total_tokens_staked`, independent of the depositor's earlier contribution — the depositor's tokens are not credited to the staker's `rewards` balance via `harvest_rewards`.
5. If stakers fully unstake again, only the pool `admin` can call `cleanup_pool` to reclaim the entire remaining reward-asset balance (including the original unprivileged depositor's donation), sending it to the admin's account — not back to the depositor. [4](#0-3)

### Citations

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L696-718)
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L843-881)
```rust
	fn create_pool(
		creator: &T::AccountId,
		staked_asset_id: T::AssetId,
		reward_asset_id: T::AssetId,
		reward_rate_per_block: T::Balance,
		expiry: DispatchTime<BlockNumberFor<T>>,
		admin: &T::AccountId,
	) -> Result<PoolId, DispatchError> {
		// Ensure the assets exist.
		ensure!(T::Assets::asset_exists(staked_asset_id.clone()), Error::<T>::NonExistentAsset);
		ensure!(T::Assets::asset_exists(reward_asset_id.clone()), Error::<T>::NonExistentAsset);

		// Check the expiry block.
		let now = T::BlockNumberProvider::current_block_number();
		let expiry_block = expiry.evaluate(now);
		ensure!(expiry_block > now, Error::<T>::ExpiryBlockMustBeInTheFuture);

		let pool_id = NextPoolId::<T>::try_mutate(|id| -> Result<PoolId, DispatchError> {
			let current_id = *id;
			*id = id.ensure_add(1)?;
			Ok(current_id)
		})?;

		let footprint = Self::pool_creation_footprint();
		let cost = T::Consideration::new(creator, footprint)?;
		PoolCost::<T>::insert(pool_id, (creator.clone(), cost));

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
```
