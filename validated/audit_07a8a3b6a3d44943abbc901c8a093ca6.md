### Title
Insufficient reward-rate precision in `pallet-asset-rewards` truncates and permanently loses staker rewards for low-decimal reward tokens - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` uses a fixed, hard-coded `PRECISION_SCALING_FACTOR` of `4096` (2^12) to scale `reward_per_token` calculations [1](#0-0) . This is the direct analog of the reported `rewardsPerSecond` precision bug: the scaling factor is not chosen relative to the reward token's decimals or the pool's staked-token magnitude, so for realistic token/pool combinations (e.g. a low-decimal reward asset paid into a pool with a large `total_tokens_staked`), integer division in `reward_per_token()` truncates to zero every block, silently and permanently under-crediting stakers.

### Finding Description
`reward_per_token()` computes the incremental reward-per-token value as:

```
reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
``` [2](#0-1) 

and `derive_rewards()` later divides back out by the same fixed `PRECISION_SCALING_FACTOR`:

```
staker.amount * (reward_per_token - staker.reward_per_token_paid) / PRECISION_SCALING_FACTOR + staker.rewards
``` [3](#0-2) 

Unlike `pallet-nomination-pools`, which uses a `FixedU128`-based `RewardCounter` (effectively 10^18 of precision) explicitly chosen and documented to avoid this class of truncation [4](#0-3) , `pallet-asset-rewards` hard-codes a tiny `u16` multiplier of `4096` with no relation to the decimals of either the staked or reward asset, and with no validation at `create_pool` or `set_pool_reward_rate_per_block` time that the chosen `reward_rate_per_block` is large enough relative to `total_tokens_staked` to survive the division [5](#0-4) [6](#0-5) .

Concretely: if `reward_rate_per_block * elapsed_blocks * 4096 < total_tokens_staked`, `reward_per_token()`'s division truncates to `0`, meaning `reward_per_token_stored` never advances for that period. Since `total_tokens_staked` is denominated in the staked asset's raw units (which can be 18-decimal, e.g. a popular fungible asset with many tokens staked) while `reward_rate_per_block` is denominated in the reward asset's raw units (which can be low-decimal, e.g. an 8-decimal wrapped-BTC-like asset with a modest rate), this is the exact scenario the external report describes for WBTC/EURS-style tokens versus `rewardsPerSecond`. The bug is entirely in the pallet's own fixed-point design, not caused by admin/governance misconfiguration — any economically reasonable, honestly-configured pool combining a large-supply staked asset with a modest-rate low-decimal reward asset triggers permanent truncation.

Once truncated to zero, `Pallet<T>::update_pool_rewards` persists `reward_per_token_stored` unchanged [7](#0-6) , so the lost reward-per-block for that period is not merely delayed — the elapsed-blocks window has already advanced `last_update_block`, so the reward for the truncated interval can never be recovered on a later, larger-elapsed calculation (it's an all-or-nothing per-call division, not a fractional-carry accumulator).

### Impact Explanation
This causes legitimate stakers to under-accrue (down to zero) their fair share of deposited reward tokens indefinitely, while `T::Assets` reward-asset balance sits in the pool's account unclaimed. This matches "permanent user-fund lock" — deposited reward tokens become effectively stuck/unspendable by their intended beneficiaries because the accounting layer can never register them as owed. It also constitutes a "runtime bug that compromises intended behavior" of the rewards accounting, independent of any admin, governance, or malicious-actor assumption.

### Likelihood Explanation
Likelihood is high for any pool pairing a modest-decimal/modest-rate reward asset with a staked asset that accumulates large `total_tokens_staked` (a very common real-world condition for popular assets, e.g. 18-decimal governance or LP tokens staked in bulk). No special privilege or malicious actor is needed to trigger the truncation — it happens automatically to any pool whose numeric parameters fall in this range, and normal `stake`/`unstake`/`harvest_rewards` calls by ordinary users are enough to expose and lock in the loss.

### Recommendation
Replace the fixed `u16` `PRECISION_SCALING_FACTOR = 4096` with a much larger, fixed-point-safe precision constant (e.g. a `FixedU128`/`u128`-based accumulator, mirroring `pallet-nomination-pools`'s `RewardCounter`), and/or validate at `create_pool`/`set_pool_reward_rate_per_block` time that `reward_rate_per_block * PRECISION_SCALING_FACTOR` is large enough relative to plausible `total_tokens_staked` for the configured staked asset's decimals, rejecting or warning on configurations that would systematically truncate to zero.

### Proof of Concept
1. Create a pool via `create_pool` (root/`CreatePoolOrigin`) with a staked asset having 18 decimals and a reward asset with a much smaller effective rate, e.g. `reward_rate_per_block = 1` (raw units) and `PRECISION_SCALING_FACTOR = 4096` [1](#0-0) .
2. Have a large staker (or many stakers) `stake()` such that `total_tokens_staked` exceeds `reward_rate_per_block * blocks_elapsed * 4096` for any realistic `blocks_elapsed` between updates (e.g. `total_tokens_staked = 10^18`, requiring `blocks_elapsed > ~2.4 * 10^14` blocks before `reward_per_token` increases at all).
3. Call `harvest_rewards` after many blocks — `reward_per_token()` computes `1 * elapsed * 4096 / 10^18`, which truncates to `0` for any practical `elapsed`, per the division at [2](#0-1) ; `staker_info.rewards` in `derive_rewards` therefore stays `0` even though reward tokens were deposited into the pool and are being paid for per the configured `reward_rate_per_block`.
4. The deposited reward-asset balance remains locked in the pool account, permanently unclaimed by stakers, confirming the fund-lock impact.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L803-809)
```rust
			Ok(pool_info.reward_per_token_stored.ensure_add(
				pool_info
					.reward_rate_per_block
					.ensure_mul(rewardable_blocks_elapsed.into())?
					.ensure_mul(PRECISION_SCALING_FACTOR.into())?
					.ensure_div(pool_info.total_tokens_staked)?,
			)?)
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L900-922)
```rust
	fn set_pool_reward_rate_per_block(
		admin: &T::AccountId,
		pool_id: PoolId,
		new_reward_rate_per_block: T::Balance,
	) -> DispatchResult {
		let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
		ensure!(pool_info.admin == *admin, BadOrigin);
		ensure!(
			new_reward_rate_per_block > pool_info.reward_rate_per_block,
			Error::<T>::RewardRateCut
		);

		// Always start by updating the pool rewards.
		let rewards_per_token = Self::reward_per_token(&pool_info)?;
		let mut pool_info = Self::update_pool_rewards(&pool_info, rewards_per_token)?;

		pool_info.reward_rate_per_block = new_reward_rate_per_block;
		Pools::<T>::insert(pool_id, pool_info);

		Self::deposit_event(Event::PoolRewardRateModified { pool_id, new_reward_rate_per_block });

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1672-1686)
```rust
		type RuntimeFreezeReason: From<FreezeReason>;

		/// The type that is used for reward counter.
		///
		/// The arithmetic of the reward counter might saturate based on the size of the
		/// `Currency::Balance`. If this happens, operations fails. Nonetheless, this type should be
		/// chosen such that this failure almost never happens, as if it happens, the pool basically
		/// needs to be dismantled (or all pools migrated to a larger `RewardCounter` type, which is
		/// a PITA to do).
		///
		/// See the inline code docs of `Member::pending_rewards` and `RewardPool::update_recorded`
		/// for example analysis. A [`sp_runtime::FixedU128`] should be fine for chains with balance
		/// types similar to that of Polkadot and Kusama, in the absence of severe slashing (or
		/// prevented via a reasonable `MaxPointsToBalance`), for many many years to come.
		type RewardCounter: FixedPointNumber + MaxEncodedLen + TypeInfo + Default + codec::FullCodec;
```
