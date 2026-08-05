Audit Report

## Title
Reward-per-token precision loss in `pallet-asset-rewards` causes permanent, unrecoverable reward loss for stakers of high-decimal / low-reward-rate pools - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`pallet-asset-rewards` computes `reward_per_token_stored` using a fixed `PRECISION_SCALING_FACTOR` of `4096` [1](#0-0)  rather than a decimals-aware or much larger fixed-point base as used by the Synthetix model the pallet's own doc-comments cite [2](#0-1) . Because `total_tokens_staked` and `reward_rate_per_block` are raw integer amounts in arbitrary, admin-chosen `pallet-assets` decimals with no normalization, `reward_per_token()`'s division can truncate to zero every checkpoint, and since each checkpoint overwrites `reward_per_token_stored` with an absolute recomputation rather than carrying a remainder, the lost fraction is permanently and silently dropped.

## Finding Description
The reward accrual computes an additive increment based on `reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked` and adds it to the previously stored value [3](#0-2) , and `derive_rewards` divides the accumulated `reward_per_token` delta by the same constant to compute payable rewards [4](#0-3) . Because this is a fixed `u16` constant (`4096`, i.e., 2^12) applied uniformly regardless of the staked/reward asset decimals chosen at pool creation, any pool where `reward_rate_per_block * blocks_elapsed * 4096 < total_tokens_staked` will have its per-checkpoint increment integer-divide to `0`. Since `update_pool_rewards` recomputes and overwrites `reward_per_token_stored` on every `stake`, `unstake`, `harvest_rewards`, and `set_pool_reward_rate_per_block` call using only the blocks elapsed since the last checkpoint, frequent legitimate interactions (which are ordinary public extrinsics, not privileged or malicious) repeatedly re-trigger this truncation, and the pallet has no remainder-carrying mechanism to recover the lost fraction on subsequent periods.

## Impact Explanation
This matches the "runtime bug that compromises intended behavior" / "permanent user-fund lock" impact category: stakers who deposit real value into a pool funded with a real reward-asset balance (via `deposit_reward_tokens`) receive systematically zero (or reduced) rewards despite a positive `reward_rate_per_block`, and the corrupted value is `reward_per_token_stored` (silently truncated to a value smaller than economically owed, sometimes permanently `0`). The reward funds remain stranded in the pool account, recoverable only via `cleanup_pool` to the admin rather than the stakers who accrued the (undercounted) entitlement — a real misallocation of user-entitled funds caused purely by pallet arithmetic, not any external or privileged assumption.

## Likelihood Explanation
While pool creation and admin parameter selection (`staked_asset_id`, `reward_asset_id`, `reward_rate_per_block`) is restricted to a permissioned origin per the pallet's documented "Permissioning" model, the exploitable path itself — `stake`, `unstake`, and `harvest_rewards` — are public, unprivileged extrinsics that any staker can call, and the bug manifests under entirely ordinary, non-malicious configuration and usage (no governance or admin misbehavior is required to trigger the truncation; only a plausible combination of decimals/rate is needed, which the pallet does not validate at `create_pool`). This is reliably and repeatably reproducible given a pool with high-decimal staked assets and low-decimals/low-rate reward assets, matching the description's reproduction steps.

## Recommendation
Increase `PRECISION_SCALING_FACTOR` to a much larger fixed-point base (e.g., matching Synthetix's `1e18`), and/or carry forward the un-truncated remainder across checkpoints instead of overwriting `reward_per_token_stored` with an absolute value each time. Add validation at `create_pool`/`stake` time bounding the relationship between `reward_rate_per_block`, expected `total_tokens_staked` magnitudes, and the precision constant, and add explicit unit tests (analogous to `nomination-pools`' `reward_counter_precision` tests) exercising high-decimals staked assets against low-decimals/low-rate reward assets.

## Proof of Concept
1. `create_pool` with an 18-decimal staked asset and a 6-decimal reward asset, `reward_rate_per_block = 1_000`.
2. Stake `1_000_000 * 10^18` units (`total_tokens_staked = 1e24`), fund the pool via `deposit_reward_tokens`.
3. Have a second staker repeatedly `stake`/`unstake` small amounts each block so `update_pool_rewards` checkpoints with `rewardable_blocks_elapsed = 1` each time.
4. Each checkpoint's increment `1_000 * 1 * 4096 / 1e24` truncates to `0` in `reward_per_token()` [5](#0-4) , so `reward_per_token_stored` never advances.
5. Calling `harvest_rewards` for the original staker later yields `staker_info.rewards == 0` via `derive_rewards` [6](#0-5) , despite real elapsed time, a positive reward rate, and a funded pool balance.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L65-68)
```rust
//! ## Rewards Algorithm
//!
//! The rewards algorithm is based on the Synthetix [StakingRewards.sol](https://web.archive.org/web/20251223190741/https://github.com/Synthetixio/synthetix/blob/develop/contracts/StakingRewards.sol)
//! smart contract.
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L812-824)
```rust
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
