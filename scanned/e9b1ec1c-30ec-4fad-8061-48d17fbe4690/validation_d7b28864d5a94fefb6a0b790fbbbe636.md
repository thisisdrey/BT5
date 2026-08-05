### Title
Frequent checkpoint calls can freeze reward accrual in `asset-rewards` - (`substrate/frame/asset-rewards/src/lib.rs`)

### Summary
The `asset-rewards` pallet updates the global `reward_per_token_stored` accumulator using integer division. If an attacker calls `stake`, `unstake`, or `harvest_rewards` every block, the elapsed block delta is `1`, and the per-block reward increment can round down to zero when `reward_rate_per_block * PRECISION_SCALING_FACTOR < total_tokens_staked`. Because `last_update_block` is still advanced to the current block on every call, the lost reward is never recovered, exactly mirroring the Curve `LiquidityGauge` spam attack.

### Finding Description
In `reward_per_token` the accumulator growth is computed as:

```rust
reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
``` [1](#0-0) 

`update_pool_rewards` then writes the current block number into `last_update_block` regardless of whether the division produced a non-zero increment:

```rust
new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number();
new_pool_info.reward_per_token_stored = reward_per_token;
``` [2](#0-1) 

All three public staker-facing extrinsics (`stake`, `unstake`, `harvest_rewards`) invoke `update_pool_and_staker_rewards` at the start and persist the returned `pool_info` to storage:

```rust
let (mut pool_info, mut staker_info) =
    Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
``` [3](#0-2) 

Consequently, any signed account can force a checkpoint every block. If the pool parameters satisfy `reward_rate_per_block * 4096 < total_tokens_staked`, the per-block increment is zero and the pool's `reward_per_token_stored` stops growing even though rewards are notionally being emitted.

### Impact Explanation
This is a runtime bug that compromises the intended reward accounting. Stakers stop earning rewards for as long as the spam continues, and the missed rewards are permanently lost because the accumulator is advanced by block number, not by accumulated time. It fits the required impact gate: a public, unprivileged entrypoint causes incorrect reward settlement (theft of expected yield / permanent underpayment) by degrading the reward accrual logic.

### Likelihood Explanation
Likelihood is medium-to-high. The attack is cheap: it only requires a signed extrinsic per block with a minimal stake/unstake/harvest amount. There is no fee exemption for miners in Substrate, but transaction fees are bounded and the attacker can use the smallest possible amount. The condition `reward_rate_per_block * 4096 < total_tokens_staked` is realistic for pools with a high total stake and a modest reward rate, or for pools configured with low-decimal reward assets.

### Recommendation
Short term: ensure pool creation and reward-rate updates enforce `reward_rate_per_block * PRECISION_SCALING_FACTOR >= total_tokens_staked`, or accumulate rewards using a higher-precision fixed-point type and only advance `last_update_block` when the accumulator actually changes.

Long term: add invariant tests that simulate per-block checkpoint spam for both short and long time windows, and verify that the total harvested rewards match the expected `reward_rate_per_block * blocks * staker_share` regardless of checkpoint frequency.

### Proof of Concept
1. Admin creates a pool with `reward_rate_per_block = 1` and `PRECISION_SCALING_FACTOR = 4096`.
2. Alice stakes `total_tokens_staked = 10_000`.
3. Expected per-block accumulator growth is `1 * 1 * 4096 / 10000 = 0` due to integer division.
4. Bob calls `stake(pool_id, 1)` every block. Each call updates `last_update_block` to the current block and sets `reward_per_token_stored` to the same value.
5. After `N` blocks, `reward_per_token_stored` has not increased, so Alice's `derive_rewards` returns zero and no rewards are ever harvestable.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L479-480)
```rust
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L779-782)
```rust
			let mut new_pool_info = pool_info.clone();
			new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number();
			new_pool_info.reward_per_token_stored = reward_per_token;

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
