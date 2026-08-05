## Analysis

The Solidity report's core broken invariant is: **reward emitted while total staked supply is zero is silently dropped instead of being retained/queued, because the checkpoint (`lastUpdateBlock`) is advanced to "now" even though no accrual happened for that idle window.** The exact same structural pattern exists in `substrate/frame/asset-rewards/src/lib.rs`.

### Title
Reward emitted during a zero-stake window is permanently dropped instead of queued in `pallet-asset-rewards` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`Pallet::reward_per_token` short-circuits and returns the unchanged `reward_per_token_stored` whenever `total_tokens_staked` is zero, while `Pallet::update_pool_rewards` unconditionally advances `last_update_block` to the current block on every call (`stake`, `unstake`, `harvest_rewards`). This means any block range during which a pool has zero stakers is silently skipped from the reward-accrual formula: the `reward_rate_per_block` tokens nominally emitted during that window are neither credited to `reward_per_token_stored` nor queued anywhere for later distribution — they simply vanish from the accounting model, exactly like the `AbstractRewarder::_updateReward()` bug where `lastUpdateBlock` advanced past a zero-`totalSupply()` window without preserving the otherwise-owed reward. [1](#0-0) [2](#0-1) 

### Finding Description
`reward_per_token()` returns early with the stored value when `total_tokens_staked.is_zero()`:

```rust
pub(super) fn reward_per_token(
    pool_info: &PoolInfoFor<T>,
) -> Result<T::Balance, DispatchError> {
    if pool_info.total_tokens_staked.is_zero() {
        return Ok(pool_info.reward_per_token_stored);
    }
    ...
}
``` [3](#0-2) 

`update_pool_rewards()`, called right after, then **always** advances `last_update_block` to the current block regardless of whether accrual happened:

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

Both `stake` and `unstake` (and `harvest_rewards`) call `update_pool_and_staker_rewards`, which chains these two functions on every invocation: [4](#0-3) [5](#0-4) [6](#0-5) 

There is no `queued_rewards` (or equivalent) field on `PoolInfo` to preserve the reward that accrues during a zero-stake window: [7](#0-6) 

Consequently, any `(now - last_update_block) * reward_rate_per_block` worth of reward tokens for a block range where `total_tokens_staked == 0` is computed as owed nowhere: `reward_per_token_stored` is not incremented for that range, and `last_update_block` is moved forward past it, so the range can never be revisited later. This is functionally identical to the reported flaw where `lastUpdateBlock` is advanced during a `totalSupply() == 0` window without accounting for the otherwise-due reward.

### Impact Explanation
Reward tokens deposited into the pool's pot (via `deposit_reward_tokens` or at `create_pool` funding) that correspond to any zero-stake block window are never credited to `reward_per_token_stored`, so no current or future staker can ever claim them through `harvest_rewards`/`unstake`. The value is not conserved to the intended beneficiaries (stakers who are due a share of `reward_rate_per_block` over the pool's active life); it becomes permanently un-attributable pool-pot balance that only the pool `admin` can later reclaim via `cleanup_pool` — and only once the pool has no stakers left. Any staker present immediately before a gap and any staker joining after it never receive the reward corresponding to the idle window, so real value is diverted away from the reward-accounting model on every pool that experiences a stake-count drop to zero. [8](#0-7) 

### Likelihood Explanation
This requires no privileged actor: any ordinary staker fully unstaking (draining `total_tokens_staked` to zero) and any subsequent `stake`/`unstake`/`harvest_rewards` call by anyone triggers `update_pool_rewards`, which advances `last_update_block` and locks in the loss for that window. This is a routine, unprivileged sequence of public extrinsic calls (`stake`, `unstake`) rather than an edge case requiring an admin, governance, or malicious relayer/validator.

### Recommendation
Track the reward accrued while `total_tokens_staked == 0` (e.g., a `queued_rewards`/`unaccounted_rewards` field on `PoolInfo`), and either (a) roll it into the first `reward_per_token_stored` increment once a staker re-appears, or (b) explicitly document/return it to the reward pot's ultimate owner in a way that is accounted for rather than silently absorbed. Do not advance `last_update_block` past a zero-stake block range without first capturing the reward emitted during that range.

### Proof of Concept
1. `create_pool` with `reward_rate_per_block = R`, staked by Alice at block 0.
2. Alice fully `unstake`s at block T1, driving `total_tokens_staked` to 0. `update_pool_rewards` sets `last_update_block = T1`, `reward_per_token_stored` reflects rewards up to T1 only (correct so far).
3. No one stakes until block T2 > T1 (gap of `T2 - T1` blocks where `total_tokens_staked == 0`).
4. Bob calls `stake` at T2. `reward_per_token()` sees `total_tokens_staked.is_zero()` (still true at the moment of computing pre-stake reward) and returns `reward_per_token_stored` unchanged; `update_pool_rewards` then sets `last_update_block = T2`.
5. The `R * (T2 - T1)` reward tokens nominally emitted for the idle window are never added to `reward_per_token_stored` and can never be retroactively attributed — `last_update_block` has already moved past that range. Those tokens remain stuck in the pool's pot account, unclaimable by Alice, Bob, or any future staker, and only recoverable by the `admin` through `cleanup_pool` once the pool becomes fully empty of stakers again.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L754-765)
```rust
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
