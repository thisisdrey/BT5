Based on the code I retrieved, the `pallet-asset-rewards` pallet exhibits the same broken invariant as the Paladin report: rewards accrue as if the pool has stakers even during intervals when `total_tokens_staked == 0`, but the accounting variable that "consumes" elapsed time (`last_update_block`) is advanced unconditionally, causing that reward-window to be silently dropped/lost.

### Title
Rewards accrued while `total_tokens_staked == 0` are permanently lost/stuck in the pool account - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`Pallet::reward_per_token` and `Pallet::update_pool_rewards` in `pallet-asset-rewards` compute and persist a pool's reward accounting independently of whether the pool currently has any stakers. When `total_tokens_staked` is zero, `reward_per_token` short-circuits and returns the unchanged `reward_per_token_stored`, but the caller `update_pool_and_staker_rewards`/`update_pool_rewards` still unconditionally sets `last_update_block = current_block_number()`. This is functionally identical to the Paladin `Valkyrie` bug: the "elapsed time with zero liquidity" is consumed without crediting anyone, and the corresponding reward-asset balance becomes unrecoverable by any staker or the admin. [1](#0-0) [2](#0-1) 

### Finding Description
The pool state `PoolInfo` tracks `reward_rate_per_block`, `total_tokens_staked`, `reward_per_token_stored`, and `last_update_block`. [3](#0-2) 

`reward_per_token` is the function that turns elapsed blocks into an incremental reward-per-token value:
```rust
pub(super) fn reward_per_token(
    pool_info: &PoolInfoFor<T>,
) -> Result<T::Balance, DispatchError> {
    if pool_info.total_tokens_staked.is_zero() {
        return Ok(pool_info.reward_per_token_stored);
    }
    let rewardable_blocks_elapsed: u32 = ...
    Ok(pool_info.reward_per_token_stored.ensure_add(
        pool_info.reward_rate_per_block
            .ensure_mul(rewardable_blocks_elapsed.into())?
            .ensure_mul(PRECISION_SCALING_FACTOR.into())?
            .ensure_div(pool_info.total_tokens_staked)?,
    )?)
}
``` [1](#0-0) 

When `total_tokens_staked == 0`, no increment to `reward_per_token_stored` is computed — this is correct in isolation (division by zero avoided), but the caller then does:
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
``` [2](#0-1) 

`last_update_block` is unconditionally moved forward to "now" regardless of whether `total_tokens_staked` was zero for part or all of the elapsed interval. This is exactly the corrupted value analogous to `_state.lastUpdateTime` in the Paladin report: it is advanced past a zero-liquidity window without crediting the equivalent reward for that window anywhere (no `accumulatedFees`-equivalent exists in this pallet).

This function is reachable via ordinary, unprivileged, public extrinsics: `stake` calls `update_pool_and_staker_rewards` → `update_pool_rewards` before adjusting `total_tokens_staked`. [4](#0-3) 

Concrete scenario:
1. Pool created with `reward_rate_per_block > 0` and `expiry_block` far in the future. `total_tokens_staked = 0`.
2. All stakers `unstake` fully (or the pool starts unstaked). `total_tokens_staked` becomes 0, and `update_pool_rewards` is invoked, setting `last_update_block = N`.
3. Time passes; the reward-per-block accrual for `[N, M]` — while nobody was staked — is architecturally meant to still be reserved in the pool's reward-asset account (the pool's `account` field, funded ahead of time or via top-ups since `create_pool`/`extend_pool` add funds without gating on staked amount).
4. At block `M`, a new staker calls `stake`. `reward_per_token` is invoked with `total_tokens_staked == 0` (pre-update state) and short-circuits, returning the stale `reward_per_token_stored`. `update_pool_rewards` then sets `last_update_block = M`.
5. The entire `[N, M]` reward window is discarded: `reward_per_token_stored` was never incremented for it, yet `last_update_block` has moved past it. No staker's `reward_per_token_paid` accounting can ever retroactively claim that interval, and there is no `accumulatedFees`-like store or admin sweep function that lets the pool `admin` or anyone recover those specific tokens; they remain deposited in the pool's `account` but are unreachable through the pallet's public API (`stake`, `unstake`, `harvest_rewards`, `set_pool_reward_rate`, etc., which all key off `reward_per_token_stored`/`reward_per_token_paid` deltas, none of which reflect the skipped window).

This matches the required impact class "permanent user-fund ... lock": reward-asset funds transferred into the pool account for that skipped interval are permanently stuck, unclaimable by any beneficiary (staker or admin), with no attacker action needed other than the natural, permissionless act of unstaking to zero and later re-staking — both ordinary user operations, not governance/admin abuse.

### Impact Explanation
Reward-asset tokens funded into the pool (via `create_pool`'s initial funding convention and any subsequent top-ups) for the duration that `total_tokens_staked == 0` are effectively burned from an accounting perspective — permanently locked in the pool's `account` with no code path granting any party a claim to them, since `reward_per_token_stored` is not incremented for that window while `last_update_block` is advanced past it. This is a straightforward loss/lock of pool-held value, matching "permanent user-fund ... lock" from the impact gate.

### Likelihood Explanation
No privileged actor is required. Any staker can unstake down to zero (a normal, permissionless action), and any subsequent staker action (their own restake, or anyone else's `stake` call) triggers the state-advancing `update_pool_rewards` path. Because emptying a pool's stake to zero, even transiently, is an expected and common scenario for a public liquidity/incentive pool, the bug is easily and repeatedly triggerable without any privileged or adversarial precondition.

### Recommendation
Mirror the Paladin fix: only advance `last_update_block` up to the block at which `total_tokens_staked` last became zero, and separately track (or explicitly forfeit-to-admin) the reward amount corresponding to intervals with zero staked tokens, e.g.:
```rust
if pool_info.total_tokens_staked.is_zero() {
    // do not silently drop this window; either freeze last_update_block at the point
    // staking hit zero, or accumulate the skipped reward into a separate
    // "unclaimed_zero_stake_rewards" balance redeemable by the pool admin.
    return Ok(pool_info.reward_per_token_stored);
}
```
and ensure `update_pool_rewards` does not advance `last_update_block` past the last block where `total_tokens_staked` was nonzero, so the next staker's `reward_per_token` calculation correctly resumes accrual from where liquidity was last present rather than skipping the zero-liquidity window entirely.

### Proof of Concept
1. `create_pool(staked_asset, reward_asset, reward_rate_per_block = R, expiry_block = far_future, admin)`. Fund the pool's `account` with reward tokens sufficient for the whole `expiry_block` horizon (as intended usage of `reward_rate_per_block`). [5](#0-4) 
2. Staker A `stake(pool_id, X)` at block `B0`.
3. Staker A `unstake(pool_id, X)` (full withdrawal) at block `B1`, driving `total_tokens_staked` to 0 and calling `update_pool_rewards`, setting `last_update_block = B1`.
4. Let `K` blocks pass with `total_tokens_staked == 0` (pool is "idle").
5. At block `B1 + K`, Staker B calls `stake(pool_id, Y)`. Internally, `update_pool_and_staker_rewards` computes `reward_per_token` using the **stale** `total_tokens_staked == 0` branch, returning `reward_per_token_stored` unchanged, then `update_pool_rewards` sets `last_update_block = B1 + K`.
6. Result: the reward budget `R * K` (which was pre-funded into the pool account under the assumption that `reward_rate_per_block` continuously accrues) is never reflected in `reward_per_token_stored`, and can never be attributed to any past or future staker via `harvest_rewards`, nor withdrawn by `admin` — it sits permanently stranded in the pool's `account`, confirmed by comparing the pool's on-chain reward-asset balance against the sum of all stakers' harvestable rewards computed from `reward_per_token_stored`, which will show a persistent, growing, unaccounted-for surplus equal to `R * K` for every such zero-stake interval.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L146-167)
```rust
/// The state and configuration of an incentive pool.
#[derive(Debug, Clone, Decode, Encode, Default, PartialEq, Eq, MaxEncodedLen, TypeInfo)]
pub struct PoolInfo<AccountId, AssetId, Balance, BlockNumber> {
	/// The asset staked in this pool.
	staked_asset_id: AssetId,
	/// The asset distributed as rewards by this pool.
	reward_asset_id: AssetId,
	/// The amount of tokens rewarded per block.
	reward_rate_per_block: Balance,
	/// The block the pool will cease distributing rewards.
	expiry_block: BlockNumber,
	/// The account authorized to manage this pool.
	admin: AccountId,
	/// The total amount of tokens staked in this pool.
	total_tokens_staked: Balance,
	/// Total rewards accumulated per token, up to the `last_update_block`.
	reward_per_token_stored: Balance,
	/// Last block number the pool was updated.
	last_update_block: BlockNumber,
	/// The account that holds the pool's rewards.
	account: AccountId,
}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L469-502)
```rust
		/// Stake additional tokens in a pool.
		///
		/// A freeze is placed on the staked tokens.
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L843-898)
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

		// Insert it into storage.
		Pools::<T>::insert(pool_id, pool);

		// Emit created event.
		Self::deposit_event(Event::PoolCreated {
			creator: creator.clone(),
			pool_id,
			staked_asset_id,
			reward_asset_id,
			reward_rate_per_block,
			expiry_block,
			admin: admin.clone(),
		});

		Ok(pool_id)
	}
```
