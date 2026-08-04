## Analog Identified: Fixed-precision reward-rate truncation in `pallet-asset-rewards`

The core broken invariant in the ManualVic report is: a per-time-unit rate is stored/consumed as a raw integer with no (or insufficient) fixed-point scaling, so for realistic parameter ranges (low rate, large denominator) the rate computation truncates to zero or to a value with large relative error, silently breaking the intended payout schedule. The direct local analog is `pallet-asset-rewards`'s `reward_per_token()` calculation, which uses a hardcoded, very small `PRECISION_SCALING_FACTOR` of `4096` to scale `reward_rate_per_block` before dividing by `total_tokens_staked`. [1](#0-0) 

### Title
Public `stake()` extrinsic can permanently truncate/zero all future reward accrual in `pallet-asset-rewards` due to undersized `PRECISION_SCALING_FACTOR` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards`'s `reward_per_token()` computes each block's reward increment as `reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked`, where `PRECISION_SCALING_FACTOR` is a fixed constant of `4096` rather than a value derived from asset decimals or configurable per pool. `stake()` is an unpermissioned, public extrinsic that any holder of the staked asset can call with an arbitrary amount, directly inflating `total_tokens_staked` used as the divisor. [2](#0-1) 

### Finding Description
`reward_per_token` derives the incremental reward-per-token as:
```
reward_per_token_stored + reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
```
using integer division with implicit floor rounding (via `ensure_mul`/`ensure_div`). [3](#0-2) 

`derive_rewards` then divides the per-staker product back down by the same `PRECISION_SCALING_FACTOR`: [4](#0-3) 

Because `PRECISION_SCALING_FACTOR` is a fixed `4096` (analogous to ManualVic's WAD/raw-unit precision problem, but even smaller), whenever `total_tokens_staked` grows large relative to `reward_rate_per_block * blocks_elapsed * 4096`, the numerator/denominator ratio truncates to `0` per update — rewards for every staker in the pool permanently stop accruing for that update window, even though `reward_rate_per_block` is nonzero and reward tokens continue to sit in the pool account. `stake()` is a fully public, unprivileged call with no minimum-precision guard or staked-amount cap relative to the reward rate, so any account holding enough of the staked asset can inflate `total_tokens_staked` to this threshold without needing admin, governance, or any privileged role. This mirrors the report's core defect: a fixed, too-low precision constant used in a per-time-unit rate calculation that becomes unusable/degenerate outside a narrow parameter range, except here it is triggerable via a normal public extrinsic rather than merely being an admin configuration mistake.

Existing guards do not stop this: `set_pool_reward_rate_per_block` only prevents rate *decreases* by the admin, and there is no check anywhere in `create_pool`, `stake`, or `reward_per_token` that bounds `total_tokens_staked` relative to `reward_rate_per_block * PRECISION_SCALING_FACTOR`. [5](#0-4) 

### Impact Explanation
Reward tokens deposited into the pool account via `deposit_reward_tokens` become effectively stranded: stakers' accrued `reward_per_token_stored` stops increasing once the truncation threshold is crossed, so legitimate stakers can no longer earn the rewards the pool was funded to distribute, while the reward tokens remain locked in the pool's sub-account (only recoverable by the admin through `cleanup_pool`, which itself requires the pool to have zero stakers). This is a permanent user-fund/reward lock condition reachable through ordinary, unprivileged pool participation.

### Likelihood Explanation
Likelihood depends on the ratio of `reward_rate_per_block` to `total_tokens_staked`; pools with low per-block reward rates or high-value/high-supply staked assets (or low decimal-precision reward tokens) are naturally close to or past the truncation threshold, and any user can push a marginal pool over that threshold simply by staking a large amount — no collusion, governance, or privileged action needed.

### Recommendation
Replace the fixed `PRECISION_SCALING_FACTOR: u16 = 4096` with a materially larger, ideally per-pool-configurable precision multiplier (e.g., a `u128`-scale WAD-like factor, or scaled by the reward asset's decimals), and add an explicit check/rejection path in `reward_per_token` (or in `stake`) when the intermediate numerator would truncate to zero relative to `total_tokens_staked`, so reward accrual degrades gracefully (e.g., accumulate remainder) instead of silently zeroing.

### Proof of Concept
1. Admin creates a pool with a modest `reward_rate_per_block` (e.g., `100`) for a staked asset with many decimals.
2. An unprivileged account calls `stake()` with an amount `total_tokens_staked` such that `total_tokens_staked > reward_rate_per_block * PRECISION_SCALING_FACTOR (4096)` for the number of blocks between updates.
3. Subsequent calls to `reward_per_token()` compute `reward_rate_per_block * blocks_elapsed * 4096 / total_tokens_staked == 0`, so `reward_per_token_stored` never increases for that staker/pool going forward, even though the pool remains funded with reward tokens via `deposit_reward_tokens`.
4. All stakers' `derive_rewards` output stops growing; reward tokens remain in the pool account, inaccessible to stakers, and only reclaimable by the admin if/when the pool becomes empty of stakers via `cleanup_pool`. [6](#0-5)

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L786-824)
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

		/// Derives the amount of rewards earned by a staker.
		///
		/// This is a helper function for `update_pool_rewards` and should not be called directly.
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
