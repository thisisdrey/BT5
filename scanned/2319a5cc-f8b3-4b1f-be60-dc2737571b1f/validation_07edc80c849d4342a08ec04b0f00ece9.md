### Title
Reward-per-token precision loss in `pallet-asset-rewards` causes permanent, unrecoverable reward loss for stakers of high-decimal / low-reward-rate pools - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes a running `reward_per_token_stored` accumulator using a fixed `PRECISION_SCALING_FACTOR` of `4096` (2^12) rather than a decimals-aware or much larger scaling constant (e.g. Synthetix-style `1e18`). Because `staked_asset_id`/`reward_asset_id` are arbitrary `pallet-assets` assets with independent, admin-chosen decimals and `reward_rate_per_block`, the numerator `reward_rate_per_block * blocks_elapsed * 4096` can be smaller than `total_tokens_staked`, causing integer division to truncate to `0`. Because each checkpoint recomputes and **overwrites** `reward_per_token_stored` (rather than carrying forward a remainder), any truncated fraction is permanently and silently lost, exactly mirroring the mechanics of the Derby `storePriceAndRewards()` bug (`nominator / denominator` losing precision due to decimal mismatch between price feed and stake, and being called incrementally each period so truncated remainders never recover).

### Finding Description
The pool reward accrual logic is: [1](#0-0) 

```rust
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
``` [2](#0-1) 

```rust
pub(super) fn reward_per_token(
    pool_info: &PoolInfoFor<T>,
) -> Result<T::Balance, DispatchError> {
    if pool_info.total_tokens_staked.is_zero() {
        return Ok(pool_info.reward_per_token_stored);
    }
    let rewardable_blocks_elapsed: u32 = ...;
    Ok(pool_info.reward_per_token_stored.ensure_add(
        pool_info
            .reward_rate_per_block
            .ensure_mul(rewardable_blocks_elapsed.into())?
            .ensure_mul(PRECISION_SCALING_FACTOR.into())?
            .ensure_div(pool_info.total_tokens_staked)?,
    )?)
}
```

and [3](#0-2) 

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

`reward_per_token()` is only recomputed and *checkpointed* into storage (via `update_pool_rewards`, which is called from `stake`, `unstake`, `harvest_rewards`, and `set_pool_reward_rate_per_block`) using the elapsed blocks *since the previous checkpoint* (`last_update_block`). Each checkpoint's increment `reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked` is stored as the new `reward_per_token_stored` — this is an absolute (non-cumulative-remainder) recomputation, so any lost fractional part from integer division is **not** carried to the next period; it is dropped forever, identical in structure to the Derby Vault's `rewardPerLockedToken[period][protocolId] = nominator / denominator` per-rebalancing-period computation.

`total_tokens_staked` and `reward_rate_per_block` are both denominated in the raw, asset-specific integer units of whatever assets the pool admin selected via `create_pool` (`Box<T::AssetId>` for both `staked_asset_id` and `reward_asset_id`, arbitrary `pallet-assets` instances) — there is no decimals normalization anywhere in this pallet. If the staked asset has high decimals (e.g. 18) relative to the reward asset's decimals/rate (e.g. a stablecoin reward at 6 decimals paid out slowly), `total_tokens_staked` in raw units dwarfs `reward_rate_per_block * blocks_elapsed * 4096`, so the division always yields `0` for the entire lifetime of the pool whenever checkpoints occur frequently (e.g. many stakers joining/leaving, or periodic `harvest_rewards` calls), even though the pool holds real reward-asset balance (`deposit_reward_tokens`) and its rate is strictly positive.

Unlike Synthetix's canonical `StakingRewards.sol` (which this pallet's own doc-comment explicitly cites as its model) that uses `1e18` fixed-point precision, `4096` provides only ~12 bits of extra precision — nowhere near enough headroom to bridge an 18-vs-6 decimals gap (`10^12` ≈ `2^40`).

### Impact Explanation
This is a public, permissionless reward-accounting bug reachable by any staker calling `stake`/`unstake`/`harvest_rewards` on a pool created with reasonable (admin-chosen, non-malicious) asset/rate parameters. It causes systematic, unrecoverable under-payment: legitimate stakers who deposited real value and a pool funded with real reward-asset balance receive `0` rewards indefinitely, while the reward tokens remain stranded in the pool account (recoverable only via `cleanup_pool`, which requires the pool to be empty of stakers and returns the balance to the *admin*, not to the stakers who earned it). This satisfies the "theft/unbacked mint/permanent user-fund lock" impact class: user-entitled reward funds become permanently unclaimable/misallocated due to a runtime bug in reward-per-token accounting, not any external protocol assumption or privileged actor.

### Likelihood Explanation
No malicious actor, governance, or privileged origin is required. Any pool where the staked asset has meaningfully more decimals than the reward asset relative to `reward_rate_per_block`, combined with ordinary usage (frequent stake/unstake/harvest activity that repeatedly checkpoints `reward_per_token_stored` with small `blocks_elapsed` windows), reliably triggers full truncation to zero. The pallet places no validation on the relationship between `reward_rate_per_block`, `total_tokens_staked` magnitude, and `PRECISION_SCALING_FACTOR` at `create_pool` time, so this can occur under normal, non-adversarial configuration and usage — exactly as documented for the Derby analog with common asset decimal combinations (e.g., an 18-decimal governance/LP staked token rewarded in a 6-decimal stablecoin).

### Recommendation
Increase `PRECISION_SCALING_FACTOR` to a much larger fixed-point base (e.g. `1e18`, matching the referenced Synthetix design and the precision level used elsewhere in the codebase, such as nomination-pools' `FixedU128`), and/or track/report the un-truncated remainder across checkpoints so fractional reward-per-token amounts are never silently dropped. Consider validating at `create_pool`/`stake` time that `reward_rate_per_block * PRECISION_SCALING_FACTOR` cannot systematically underflow relative to plausible `total_tokens_staked` ranges, and add explicit test coverage (as `nomination-pools` does with its `reward_counter_precision` module) exercising high-decimals staked assets against low-decimals/low-rate reward assets to catch full-truncation scenarios.

### Proof of Concept
1. `create_pool` with `staked_asset_id` = an 18-decimal asset, `reward_asset_id` = a 6-decimal stablecoin-like asset, `reward_rate_per_block` = `1_000` (i.e. `0.001` reward-asset units/block).
2. Stake `1_000_000 * 10^18` of the staked asset (`total_tokens_staked = 1e24`).
3. `deposit_reward_tokens` with a large real balance so the pool is funded.
4. Have stakers interact every block (e.g. a second staker repeatedly `stake`/`unstake`s tiny amounts), so each `update_pool_rewards` checkpoint sees `rewardable_blocks_elapsed = 1`.
5. Compute increment: `1_000 * 1 * 4096 / 1e24 = 4_096_000 / 1e24 → 0` (integer division truncates to zero every single checkpoint).
6. `reward_per_token_stored` never advances; call `harvest_rewards` for the original staker at any later block — `staker_info.rewards` and the transferred `amount` are `0`, despite real elapsed time, real reward-rate, and real funded reward balance in the pool account.

### Citations

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
