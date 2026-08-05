The code matches the claim exactly as described. Let me verify the constant definition and other details.The claim is fully verified against the actual code: `PRECISION_SCALING_FACTOR` is a fixed `u16 = 4096` constant [1](#0-0) , used identically in `reward_per_token`'s accrual calculation [2](#0-1)  and reversed in `derive_rewards` [3](#0-2) . `create_pool` only checks asset existence with no decimals relationship enforced [4](#0-3) , and `update_pool_rewards` unconditionally advances `last_update_block` regardless of whether the computed increment truncated to zero [5](#0-4) . This is a genuine runtime bug compromising the pallet's intended reward-accounting behavior via integer-division truncation to zero when decimal precision gaps between staked and reward assets are large relative to the fixed constant — a case not covered by the existing zero-total-staked guard [6](#0-5) .

Audit Report

## Title
Fixed `PRECISION_SCALING_FACTOR` in `pallet-asset-rewards` causes silent, unrecoverable loss of staker rewards when staked/reward asset decimals diverge - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`pallet-asset-rewards` uses a single hard-coded constant `PRECISION_SCALING_FACTOR = 4096` to scale the division in `reward_per_token` that converts `reward_rate_per_block * blocks_elapsed` (denominated in reward-asset units) into a per-staked-token accrual rate (denominated relative to `total_tokens_staked`, in staked-asset units). Since `staked_asset_id` and `reward_asset_id` are independently chosen with no decimals relationship enforced, realistic decimal gaps between the two assets (e.g., 18-decimal staked LP token vs 6-decimal reward stablecoin) cause the scaled numerator to be smaller than the denominator, making integer division floor to zero while `last_update_block` still advances, permanently discarding that period's reward accrual.

## Finding Description
`reward_per_token` computes `reward_per_token_stored += reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked` using `ensure_mul`/`ensure_div` for overflow/div-by-zero safety only, with no check for precision truncation to zero. `derive_rewards` later divides by the same fixed `PRECISION_SCALING_FACTOR` to recover the staker's actual reward-asset amount. `create_pool` performs no validation relating `staked_asset_id` decimals to `reward_asset_id` decimals or to `reward_rate_per_block`, so a pool admin can freely configure combinations where `reward_rate_per_block * blocks_elapsed * 4096` is many orders of magnitude smaller than `total_tokens_staked`. The only existing guard in `reward_per_token` checks `total_tokens_staked.is_zero()`, which does not catch the case where the total is merely large relative to the scaled numerator. Critically, `update_pool_rewards` unconditionally sets `last_update_block` to the current block regardless of whether the newly computed `reward_per_token` actually changed, so once a truncation-to-zero occurs, the elapsed-block window is consumed and its intended reward accrual can never be recovered on a subsequent call.

## Impact Explanation
This is a runtime bug that compromises the pallet's intended reward-distribution behavior: stakers permanently lose access to reward-asset amounts that the pool's `reward_rate_per_block` and funding were configured to pay out, purely due to a dimensional-analysis flaw in the fixed-point scaling constant, with no attacker action beyond normal pool creation and staking required. This matches the "runtime bugs that compromise intended behavior" and "unbacked value drift" category of impact for a value-distributing pallet usable by any parachain adopting `pallet-asset-rewards`.

## Likelihood Explanation
No privileged or malicious action is needed — any pool creator (gated only by `CreatePoolOrigin`, which does not constrain asset decimal combinations) choosing realistic asset pairs with differing decimals (a common real-world scenario, e.g., an 18-decimal LP token staked against a 6-decimal stablecoin reward) will trigger this deterministically given normal `reward_rate_per_block` and stake magnitudes, since 4096 is negligible compared to the 10^12–10^18 scale gaps that occur naturally between live tokens of differing decimals.

## Recommendation
Replace the single pallet-wide fixed `PRECISION_SCALING_FACTOR` with either: (a) a per-pool scaling factor derived at `create_pool` time from the actual decimals of `staked_asset_id` and `reward_asset_id`, stored in `PoolInfo` and used consistently in both `reward_per_token` and `derive_rewards`; or (b) a much larger fixed-point base (e.g., 1e18) combined with wider intermediate arithmetic, plus an explicit check/event to detect and surface truncation-to-zero conditions so pool admins can react before reward accrual windows are silently lost.

## Proof of Concept
1. Create a pool via `create_pool` with `staked_asset_id` an 18-decimal asset, `reward_asset_id` a 6-decimal asset, and `reward_rate_per_block = 1_000_000`.
2. A staker calls `stake` with `500_000 * 10^18` raw units, making `total_tokens_staked = 5e23`.
3. After one block, `reward_per_token` computes numerator `1_000_000 * 1 * 4096 = 4.096e9`, which divided by `5e23` floors to `0` via `ensure_div`.
4. `reward_per_token_stored` remains unchanged while `update_pool_rewards` advances `last_update_block` to the current block, permanently discarding that block's intended reward accrual for all stakers in the pool.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L790-792)
```rust
			if pool_info.total_tokens_staked.is_zero() {
				return Ok(pool_info.reward_per_token_stored);
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L850-853)
```rust
	) -> Result<PoolId, DispatchError> {
		// Ensure the assets exist.
		ensure!(T::Assets::asset_exists(staked_asset_id.clone()), Error::<T>::NonExistentAsset);
		ensure!(T::Assets::asset_exists(reward_asset_id.clone()), Error::<T>::NonExistentAsset);
```
