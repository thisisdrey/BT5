### Title
Fixed `PRECISION_SCALING_FACTOR` in `pallet-asset-rewards` causes silent, unrecoverable loss of staker rewards when staked/reward asset decimals diverge - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
The external report identifies a dimensional-analysis flaw: a fixed scaling constant is used to reconcile two rates whose precision depends on the decimals of two *different* tokens, and that constant does not account for the decimals gap, causing the reconciled value to be wrong whenever the two tokens' decimals differ. The exact same bug class exists in `pallet-asset-rewards`'s `reward_per_token` accrual math, where a single hard-coded constant `PRECISION_SCALING_FACTOR = 4096` [1](#0-0)  is used to scale a division whose two operands (`reward_rate_per_block`, denominated in the reward asset's units, and `total_tokens_staked`, denominated in the staked asset's units) can have arbitrarily different decimal precisions, since `staked_asset_id` and `reward_asset_id` are independently chosen `AssetId`s with no decimals relationship enforced anywhere.

### Finding Description
`reward_per_token` computes the pool's cumulative reward-per-staked-token as:

```
reward_per_token_stored += reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
``` [2](#0-1) 

`derive_rewards` later removes the same fixed scaling factor to convert a staker's `amount * Δreward_per_token` back into actual reward-asset units:

```
rewards += staker.amount * (reward_per_token - reward_per_token_paid) / PRECISION_SCALING_FACTOR
``` [3](#0-2) 

`PRECISION_SCALING_FACTOR` is a single constant (4096, i.e. ~2^12) that is applied uniformly regardless of the decimals of `staked_asset_id` or `reward_asset_id`. Both assets are supplied by the pool creator with no decimals check or relationship constraint (`create_pool` only verifies the assets exist) [4](#0-3) . This is precisely the pattern flagged in the report: two rate-like quantities (`reward_rate_per_block` and the staked total) carry different intrinsic decimal precisions, and a single fixed scale factor is used to reconcile them assuming they match — but nothing guarantees they do.

When `total_tokens_staked` is denominated in a high-decimal asset (e.g. an 18-decimal LP token, so realistic totals are `~1e18` to `~1e24` in raw units) while `reward_rate_per_block` is denominated in a low/medium-decimal reward asset (e.g. 6 or 10 decimals, with realistic per-block rates of `1e6`–`1e9` raw units), the numerator `reward_rate_per_block * blocks_elapsed * 4096` can be several orders of magnitude smaller than `total_tokens_staked`. Because `ensure_div` performs integer (floor) division, the entire computed increment truncates to `0`, yet `update_pool_rewards` unconditionally advances `last_update_block` to the current block [5](#0-4) . The elapsed-blocks window for that period is thereby consumed and can never be recomputed later — the reward-asset value the pool was funded to distribute during that window is permanently and silently lost from stakers' accrual, even though the pool account is being drained of nothing (funds sit idle) or, depending on subsequent parameter changes, are later distributed disproportionately once `total_tokens_staked` shrinks (e.g., after large withdrawals), inflating the effective reward-per-token for whichever staker happens to be present at that moment relative to what was actually funded.

Existing guards do not prevent this: `reward_per_token` only special-cases `total_tokens_staked == 0` (returns unchanged) [6](#0-5) ; it performs no check that the scaled numerator is non-trivial relative to the denominator, and `ensure_mul`/`ensure_div` only guard against overflow/div-by-zero, not against precision truncation to zero. There is no per-pool or per-asset-pair scaling parameter — the constant is pallet-wide and immutable.

### Impact Explanation
This compromises the pallet's intended reward-accounting behavior: any pool whose staked-asset decimals are meaningfully larger than what the fixed 4096 scaling can absorb relative to the configured `reward_rate_per_block` will systematically under-accrue (in the extreme, zero-accrue) staker rewards for extended periods, while the pool admin still believes the configured `reward_rate_per_block` is being honestly distributed. This is a runtime bug that compromises intended behavior of a value-distributing pallet usable by any parachain adopting `pallet-asset-rewards` — it results in unbacked-value drift between what the pool is configured/funded to pay and what stakers can actually claim, and in edge cases (total staked oscillating near the truncation boundary) it can cause uneven/incorrect settlement between stakers who happen to interact at different total-staked levels.

### Likelihood Explanation
No malicious actor, admin abuse, or privileged action is required — any permissionless pool creator/admin (pool creation is gated by `CreatePoolOrigin`, but reward-asset/staked-asset decimal combinations are otherwise unconstrained) or governance choosing a realistic asset pair with common decimal conventions (e.g., 18-decimal LP token staked, 6-decimal stablecoin reward, or any pairing where `reward_rate_per_block * PRECISION_SCALING_FACTOR` is small relative to typical total-staked magnitude) will trigger this deterministically, not merely as a theoretical edge case, because 4096 is a very small fixed-point multiplier compared to the 10^12–10^18 scale gaps that occur naturally across live tokens.

### Recommendation
Do not use a single pallet-wide fixed `PRECISION_SCALING_FACTOR`. Instead, either (a) derive the scaling factor per pool at `create_pool` time from the actual decimals of `staked_asset_id` and `reward_asset_id` (analogous to the report's recommendation to scale `previewRateAfterDeposit`'s output by `10^(dL−dI)`), storing it in `PoolInfo` and using it consistently in `reward_per_token`/`derive_rewards`, or (b) widen the intermediate arithmetic (e.g., use a much larger fixed-point base, such as 1e18, and/or an intermediate wider integer type) so that realistic decimal gaps between staked and reward assets cannot truncate the numerator to zero, and add an explicit check/event when truncation would occur so admins can detect under-accrual instead of it happening silently.

### Proof of Concept
1. `CreatePoolOrigin` creates a pool with `staked_asset_id` = an 18-decimal asset and `reward_asset_id` = a 6-decimal asset, `reward_rate_per_block` = `1_000_000` (i.e., 1.0 reward token/block) via `create_pool` [7](#0-6) .
2. A staker stakes `500_000 * 10^18` raw units of the staked asset via `stake` [8](#0-7) , so `total_tokens_staked = 5e23`.
3. One block elapses. `reward_per_token` computes numerator = `1_000_000 * 1 * 4096 = 4.096e9`; dividing by `total_tokens_staked = 5e23` floors to `0` [9](#0-8) .
4. `reward_per_token_stored` is unchanged, but `last_update_block` advances to the current block [5](#0-4) , permanently discarding that block's intended reward accrual for every staker in the pool — reward asset that should have been claimable never becomes claimable, while the pool's `reward_rate_per_block` configuration and pool balance suggest it should be.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L843-853)
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
```
