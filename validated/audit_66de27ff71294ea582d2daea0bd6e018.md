## Title
`unstake` can permanently lock a pool's rewards when a low-precision staking asset drives `reward_per_token` past `Balance::MAX`, and no unprivileged recovery path exists - ([File: substrate/frame/asset-rewards/src/lib.rs])

### Summary
`pallet-asset-rewards`'s `reward_per_token` (the direct analog of AuraLocker's `_rewardPerToken()`) computes a monotonically-growing accumulator using checked arithmetic that returns `Error::Overflow` instead of wrapping. That is a correctness improvement over the original Solidity bug, but it reproduces the exact same *liveness* failure: once `reward_per_token_stored` for a pool grows large enough that a subsequent `ensure_mul`/`ensure_add` overflows `T::Balance`, every extrinsic that must call `update_pool_and_staker_rewards`/`reward_per_token` before touching pool state (`stake`, `unstake`, `harvest_rewards`) will return `Err` and permanently revert. Unlike Aura's `AuraLocker`, this pallet has no `shutdown()`/`emergencyWithdraw()` escape hatch, so staked funds and pending rewards become permanently stuck in the pool with no unprivileged (or even privileged) call able to unwind them.

### Finding Description
`reward_per_token` in `substrate/frame/asset-rewards/src/lib.rs` mirrors AuraLocker's `_rewardPerToken()` formula exactly: [1](#0-0) 

```
reward_per_token_stored + reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
```

This is called from `update_pool_and_staker_rewards`, which is invoked (per the pallet's own design note that all mutating logic funnels through it) at the top of `stake`, `unstake`, and `harvest_rewards`: [2](#0-1) 

The staking asset (`staked_asset_id`) and reward rate (`reward_rate_per_block`) are freely chosen by whoever has `CreatePoolOrigin`, and pool parameters such as `reward_rate_per_block` can subsequently be raised further by the pool `admin` (this is documented pallet behavior: "the pool admin may increase reward rate per block"). If the staking asset has few decimals (e.g. a 6-decimal stablecoin-style asset) and `total_tokens_staked` is small relative to `reward_rate_per_block * PRECISION_SCALING_FACTOR (4096)`, the numerator can grow to overflow `T::Balance` well before `expiry_block`, exactly mirroring the original PoC where `totalSupply = 1` and `rewardRate ≈ 1e12`.

Once this happens, `reward_per_token` returns `Err(Error::Overflow)` on every future call, because `reward_per_token_stored` (the accumulator) has already been persisted at a value that, combined with any further elapsed blocks, overflows on `ensure_add`/`ensure_mul`. Since `reward_per_token_stored` is monotonically increasing and only decreases never, this is a one-way trap: it never becomes callable again.

Critically, `update_pool_rewards`/`update_pool_and_staker_rewards` are pure, no-side-effect helper functions (as the module doc explicitly states), so there is no separate "reset" storage mutation that could unwind `reward_per_token_stored`. There is also no `shutdown`-equivalent extrinsic in this pallet to bypass reward accounting and let stakers withdraw their staked (non-reward) principal. Every path that would let a user recover their frozen staking-asset balance (`unstake`) is gated behind the same reward computation that now always errors.

### Impact Explanation
This is a permanent user-fund lock: once triggered, both the staked principal (frozen via `AssetsFreezer`) and any accrued-but-unclaimed reward-asset balance become permanently unrecoverable for every staker in the affected pool, since `unstake` and `harvest_rewards` both fail deterministically for all callers, forever. This matches the "permanent user-fund or bridge-state lock" impact category explicitly in scope.

### Likelihood Explanation
The pool creator/admin does not need to be malicious in an adversarial sense to trigger this - it can occur from ordinary pool configuration choices (a low-decimal staking asset plus a reasonably aggressive `reward_rate_per_block`) combined with normal churn in `total_tokens_staked` as stakers unstake, shrinking the divisor over time and increasing the per-block per-token increment. Because `PRECISION_SCALING_FACTOR` (4096) further inflates the numerator versus the original Synthetix/Aura design (which used 1e18), the margin to overflow is *smaller*, not larger, for equivalent-magnitude balances. No governance or admin abuse is required as the root cause - the trigger is a foreseeable interaction between pool configuration and organic supply/stake fluctuation, not a compromised actor.

### Recommendation
- Use a wider intermediate type (e.g. `U256`/`u128`-widened arithmetic, similar to `nomination-pools`' `BalanceToU256`/`U256ToBalance` pattern used in `balance_to_point`/`point_to_balance`) for the `reward_per_token` numerator before dividing, rather than performing the multiplication in `T::Balance` directly.
- Add a privileged/administrative recovery path (analogous to `AuraLocker::shutdown()`) that lets stakers withdraw their staked principal (and ideally settle rewards at a last-good `reward_per_token_stored` checkpoint) even if the JIT reward computation would overflow.
- Consider bounding `reward_rate_per_block` relative to `total_tokens_staked` at pool-parameter-change time to make this class of overflow unreachable by construction.

### Proof of Concept
1. `CreatePoolOrigin` creates a pool with `staked_asset_id` = a 6-decimal asset, `reward_asset_id` = an 18-decimal asset, and `reward_rate_per_block` chosen such that `reward_rate_per_block * PRECISION_SCALING_FACTOR (4096)` is large relative to typical `total_tokens_staked`.
2. A single staker calls `stake` with a small amount, making `total_tokens_staked` small (e.g. 1 unit).
3. Blocks elapse. On the next call to any of `stake`/`unstake`/`harvest_rewards`, `update_pool_and_staker_rewards` → `reward_per_token` computes `reward_rate_per_block.ensure_mul(blocks_elapsed).ensure_mul(PRECISION_SCALING_FACTOR).ensure_div(total_tokens_staked)` and adds it to `reward_per_token_stored` via `ensure_add`; once this exceeds `T::Balance::MAX`, the call returns `Err(Error::Overflow)` and `reward_per_token_stored` (already persisted from a prior successful update) locks the pool state.
4. From this point forward, every staker's `unstake` and `harvest_rewards` call for this pool fails with the same overflow error, permanently freezing their staked assets and unclaimed rewards, with no pallet-level recovery function available. [3](#0-2)

### Citations

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
