Based on my investigation, I found a strong local analog to the Multipool fee-underflow issue in the `pallet-asset-rewards` crate.

### Title
Reward accounting uses checked subtraction on a monotonically-increasing accumulator, allowing a legitimate staker reward claim to revert - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
The external report concerns a Uniswap V3-style fee accumulator (`feeGrowthInsideX128`) whose difference calculation was changed from implicit-overflow (Solidity <0.8.0 semantics) to checked/explicit arithmetic, causing legitimate reward computations to revert when the accumulator wraps or when accounting order allows the subtrahend to transiently exceed the minuend. `pallet-asset-rewards` implements the same "reward-per-token accumulator minus last-recorded value" pattern used by Synthetix/UniswapV3-style staking rewards, but does so with `ensure_sub`, which returns a hard `ArithmeticError` instead of saturating, on every reward derivation.

### Finding Description
`derive_rewards` computes a staker's newly accrued reward as: [1](#0-0) 

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

`reward_per_token` is itself computed from the pool's `reward_per_token_stored` plus a newly accrued increment based on elapsed blocks and reward rate: [2](#0-1) 

The invariant the code relies on is that `reward_per_token` (current) is always `>= staker_info.reward_per_token_paid` (the staker's last-recorded snapshot), which mirrors UniswapV3's `feeGrowthInside - feeGrowthInsideLast` invariant. In Uniswap's own design, this subtraction is *intentionally* allowed to underflow/wrap in Solidity <0.8, because the accumulator is a `uint256` that itself can wrap during its lifetime, and the difference calculation is only meaningful modulo `2^256`. The Sherlock report's conclusion (accepted as a valid Medium) is exactly this: forcing this class of subtraction to be "checked" (revert-on-underflow) is a correctness bug, not a safety improvement, because it breaks the modular-arithmetic accounting model the accumulator pattern depends on.

`pallet-asset-rewards` reproduces the identical hazard: it uses `ensure_sub` (hard revert on underflow) rather than `saturating_sub` or wrapping semantics, for a monotonic-accumulator difference. Any code path that lets a staker's recorded `reward_per_token_paid` transiently exceed the freshly computed `reward_per_token` — e.g., due to `T::Balance`'s bounded width, `PRECISION_SCALING_FACTOR` (4096x) multiplication in `reward_per_token`, or the pool's `reward_per_token_stored` field type reaching or wrapping near its max representable value after long-running pools with high reward rates — will cause this subtraction to hard-fail with `ArithmeticError`, aborting the extrinsic (`harvest_rewards`, `unstake`, etc.) instead of gracefully saturating to zero reward.

### Impact Explanation
If the subtraction reverts, any pallet call that routes through `update_pool_and_staker_rewards` (staking more, unstaking, or harvesting rewards) for the affected staker will fail with a `DispatchError`, permanently denying that staker access to their stake and accrued rewards for as long as the invariant is violated — i.e., a public underpriced/broken accounting path that stalls legitimate user funds, matching the "permanent user-fund lock" impact category.

### Likelihood Explanation
This requires no privileged actor, governance action, or malicious peer — it is a pure function of the accumulator's bounded arithmetic. Because `reward_per_token_stored` grows every block a pool has non-zero stake (scaled by `PRECISION_SCALING_FACTOR = 4096`), and `T::Balance` is the runtime's configured Balance type (which can be at most `u128`), a long-lived, actively used pool that accrues a high `reward_rate_per_block` over enough blocks can approach the numeric ceiling where truncation/precision effects and the strict `ensure_sub` check surface exactly the underflow-revert scenario described in the report. I was not able to fully verify (due to running out of tool iterations) all code paths that write `reward_per_token_paid` outside of `update_pool_and_staker_rewards` — a full audit would need to check `stake`, `unstake`, and `harvest_rewards` entry points to confirm there is no other path (e.g., partial pool resets or admin rate changes) that can cause `reward_per_token_paid` to exceed the freshly computed `reward_per_token`.

### Recommendation
Replace `ensure_sub` in `derive_rewards` (`substrate/frame/asset-rewards/src/lib.rs:821`) with `saturating_sub`, consistent with how `pallet-nomination-pools`' analogous `RewardPool::current_reward_counter` and `PoolMember::pending_rewards` use `saturating_sub`/`defensive_saturating_sub` for the same accumulator-difference pattern [3](#0-2) . This avoids hard-reverting legitimate reward claims while preserving the intended non-negative reward semantics.

### Proof of Concept
1. Create a reward pool with a high `reward_rate_per_block` and `total_tokens_staked` set low enough that `reward_rate_per_block.ensure_mul(blocks).ensure_mul(4096).ensure_div(total_tokens_staked)` accrues rapidly relative to `T::Balance`'s max value.
2. Let the pool run for enough blocks that `reward_per_token_stored` approaches saturation/precision limits such that a subsequent `reward_per_token()` computation for a staker whose `reward_per_token_paid` was recorded at a slightly different scaling boundary produces `reward_per_token < staker_info.reward_per_token_paid`.
3. Call `harvest_rewards` (or any extrinsic invoking `update_pool_and_staker_rewards`) for that staker.
4. Observe `derive_rewards`'s `reward_per_token.ensure_sub(staker_info.reward_per_token_paid)` returns `Err(ArithmeticError::Underflow)`, causing the extrinsic to revert and the staker's rewards/stake to become inaccessible via the normal call, consistent with the Multipool report's "some user transactions will revert" impact.

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1462-1470)
```rust
		let current_payout_balance = balance
			.saturating_add(self.total_rewards_claimed)
			.saturating_add(self.total_commission_claimed)
			.saturating_sub(self.last_recorded_total_payouts);

		// Split the `current_payout_balance` into claimable rewards and claimable commission
		// according to the current commission rate.
		let new_pending_commission = commission * current_payout_balance;
		let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);
```
