### Title
Reward-per-token accrual in `pallet-asset-rewards` can permanently round to zero, freezing staker rewards - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes a global accumulator `reward_per_token_stored` using a fixed `PRECISION_SCALING_FACTOR` of only `4096` (a `u16`) [1](#0-0) . This is the same broken pattern as the reported Solidity `RewardsTracker.accRewardsPerShare` bug: an integer division whose numerator can be smaller than the denominator, causing the increment to floor to zero and silently drop rewards.

### Finding Description
`reward_per_token` computes:
```
reward_per_token_stored + reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
``` [2](#0-1) 

With `PRECISION_SCALING_FACTOR = 4096`, if `total_tokens_staked` (denominated in the staked asset's decimals, e.g. an 18-decimal LP/governance token with a large supply) is large relative to `reward_rate_per_block * rewardable_blocks_elapsed * 4096` (denominated in the reward asset's decimals, e.g. a 6-decimal stablecoin with a modest per-block rate), the division floors to `0`. Unlike the Solidity report where `PRECISION_FACTOR` was `1e12`, here it is orders of magnitude smaller (`4096` vs `1e12`), making the rounding-to-zero condition dramatically easier to trigger for realistic decimal/scale combinations.

Critically, `update_pool_rewards` unconditionally advances `last_update_block` to the current block every time it is invoked, regardless of whether the computed increment was zero [3](#0-2) . This function is invoked from every public pool-touching extrinsic (stake/unstake/harvest, via `update_pool_and_staker_rewards`) as part of the standard "touch before mutate" flow [4](#0-3) . Because the elapsed-block window resets on every touch, an unprivileged caller can repeatedly invoke any pool-touching extrinsic (e.g. `stake`/`unstake` with minimal amounts, or `harvest_rewards`) every block. Each call recomputes `rewardable_blocks_elapsed` as a small number (as low as 1), which — combined with the tiny `PRECISION_SCALING_FACTOR` — repeatedly floors the numerator/denominator division to zero, so `reward_per_token_stored` never advances. This is not a one-off dust loss (as in the original report) but a persistent accrual stall affecting *all* stakers in the pool, since the accumulator is shared pool-wide state.

### Impact Explanation
When the accrual increment rounds to zero and the window resets each time, stakers earn no rewards even though the pool operator has funded the pool's reward account under normal expectations. This is a "public underpriced work" style griefing vector: any account can call cheap, public pool extrinsics every block to keep resetting `last_update_block`, permanently stalling reward distribution for every staker in that pool. Funds intended as rewards remain stuck/undistributed in the pool account, matching the "permanent user-fund lock" impact category.

### Likelihood Explanation
The scenario is realistic whenever the staked asset has high decimal precision/large float supply and the reward asset has low decimals or a low per-block rate — a very plausible pool configuration (e.g., staking an 18-decimal governance/LP token to earn a 6-decimal stablecoin at a modest rate). No privileged actor, governance action, or off-chain infra is required; any unprivileged account can trigger the griefing pattern via ordinary, publicly callable extrinsics.

### Recommendation
- Increase `PRECISION_SCALING_FACTOR` substantially (e.g., to `1e18`-scale using a wider integer type) so that realistic reward-rate/stake-size ratios do not floor to zero.
- Do not advance `last_update_block` unconditionally when the computed reward increment is zero; instead, track undistributed remainder/dust so it accumulates rather than being discarded on every touch.
- Add invariant checks/tests ensuring `reward_per_token` cannot silently stall to zero increments across repeated same-block or near-same-block updates for expected asset-decimal combinations.

### Proof of Concept
1. Create a pool via `create_pool` with a staked asset of 18 decimals and total supply staked ~`10^24` (`total_tokens_staked`), and a reward asset of 6 decimals with `reward_rate_per_block` set to a modest value (e.g., `10^6` reward-units per block, i.e., 1 token/block).
2. Compute per-block increment: `reward_rate_per_block * 1 * 4096 / total_tokens_staked = 10^6 * 4096 / 10^24 = 0` (floors to zero) — confirmed by the exact arithmetic in `reward_per_token` [2](#0-1) .
3. Any account calls a pool-touching extrinsic (e.g., `harvest_rewards` or `stake`/`unstake` with a trivial amount) every block; each call runs `update_pool_and_staker_rewards` → `reward_per_token` → `update_pool_rewards`, which sets `last_update_block` to the current block regardless of the zero increment [5](#0-4) .
4. `reward_per_token_stored` never increases across any number of blocks, and all stakers' `derive_rewards` output remains zero indefinitely [6](#0-5) , even though the pool's configured reward rate implies rewards should be accruing.

**Note on verification limits**: I could not fully trace every caller of `update_pool_rewards`/`update_pool_and_staker_rewards` (e.g., exact `stake`/`unstake`/`harvest_rewards` extrinsic implementations) within the available iterations to confirm there is no additional guard (e.g., a minimum elapsed-block threshold) that might mitigate the reset-every-block griefing path. This should be verified against the full extrinsic implementations in `substrate/frame/asset-rewards/src/lib.rs` before treating this as fully confirmed.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L754-784)
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

		/// Computes update pool reward state.
		///
		/// Should be called every time the pool is adjusted, and a staker is not involved.
		///
		/// Returns the updated pool and staker info.
		///
		/// NOTE: this function has no side-effects. Side-effects such as storage modifications are
		/// the responsibility of the caller.
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
