This confirms a strong, directly provable local analog to the SymmStaking H-1 bug in `substrate/frame/asset-rewards`. I have enough evidence to answer.

### Title
Reward-per-token griefing via low `PRECISION_SCALING_FACTOR` permanently zeroes staker rewards in `pallet-asset-rewards` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` implements a Synthetix-style JIT reward accumulator (`reward_per_token`) that is updated on every `stake`, `unstake`, and `harvest_rewards` call. Just like the reported SymmStaking bug, the per-token increment is scaled by a fixed multiplier before dividing by `total_tokens_staked`, and if the increment rounds down to zero, `last_update_block` still advances and that period's rewards are permanently lost. The multiplier used here, `PRECISION_SCALING_FACTOR = 4096`, is drastically smaller than the `1e18` FixedPoint precision used elsewhere in the runtime (e.g. nomination-pools' `RewardCounter`), making the rounding-to-zero griefing far easier to trigger than in the original report.

### Finding Description
`reward_per_token` computes:
```
reward_per_token_stored + reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
``` [1](#0-0) 

`PRECISION_SCALING_FACTOR` is a constant `4096` (2^12), far weaker than the `1e18`-scale fixed point used by comparable reward-distribution logic elsewhere in the codebase: [2](#0-1) 

`update_pool_rewards` unconditionally advances `last_update_block` to the current block and overwrites `reward_per_token_stored` with whatever `reward_per_token()` computed — including a value that is unchanged due to integer-division underflow to zero: [3](#0-2) 

Every `stake`, `unstake`, and `harvest_rewards` extrinsic is permissionless (any signed account for `stake`; the staker themself, or anyone after expiry, for `unstake`/`harvest_rewards`) and unconditionally calls `update_pool_and_staker_rewards` before mutating state: [4](#0-3) [5](#0-4) 

There is no minimum interval, no dust-accumulation carryover, and no accuracy safeguard on `total_tokens_staked` size vs `reward_rate_per_block`, so an attacker can call `stake`/`unstake` with a trivial amount (or even 0-effect no-op amounts) every block to keep `total_tokens_staked` large and `blocks_elapsed` small, driving the per-token increment to zero on every update while `last_update_block` still advances — permanently consuming that period's reward accrual for every honest staker in the pool.

### Impact Explanation
When triggered, `reward_per_token_stored` never increases even though the pool account continues to hold reward tokens transferred in via `deposit_reward_tokens`/direct transfer. Honest stakers accrue zero rewards despite the pool being funded, and the reward tokens become effectively stuck (only recoverable by the pool admin via `cleanup_pool` once the pool is emptied of stakers). This is a direct, unbacked-loss/fund-lock condition matching the "theft or unbacked mint or unlock" / "permanent user-fund ... lock" impact classes, executable purely by an unprivileged staker with no governance, relayer, or validator involvement.

### Likelihood Explanation
High. Any account can call `stake`/`unstake` for pools they participate in every block at negligible cost (freeze/unfreeze of a tiny staked-asset amount), and the vulnerability is structural — it requires no special conditions beyond a sufficiently large `total_tokens_staked` relative to `reward_rate_per_block * PRECISION_SCALING_FACTOR`, which is a normal/expected configuration for pools rewarding low-decimal or low-rate assets against an 18-decimal staked asset (directly analogous to SYMM/USDC in the source report).

### Recommendation
Increase `PRECISION_SCALING_FACTOR` to at least `10^18` (matching `FixedU128`-level precision used elsewhere, e.g. nomination-pools' reward counter) and use wide (`U256`) intermediate arithmetic for the multiplication before dividing, as is already done in `point_to_balance` elsewhere in the runtime [6](#0-5) . Additionally, consider tracking undistributed remainder/dust explicitly rather than silently dropping it when `reward_per_token` rounds down, and/or rate-limiting how often `last_update_block` can be advanced without a nonzero per-token increment.

### Proof of Concept
1. Admin creates a pool with `staked_asset_id` = an 18-decimal asset and `reward_asset_id` = a 6-decimal asset (e.g. USDC-style), with `reward_rate_per_block` set so weekly rewards total ~1209.6 (6-decimal) units over ~100,800 blocks (6s blocks): `reward_rate_per_block ≈ 12000` raw units.
2. Total staked in the pool reaches `1_000_000e18`.
3. Attacker calls `stake(pool_id, 1)` (or `unstake`) every block. Each call triggers `reward_per_token()`:
   `12000 * 1 * 4096 / 1_000_000e18 ≈ 4.9e-17` → rounds to `0` in integer arithmetic.
4. `update_pool_rewards` still sets `last_update_block = now`, so that block's reward accrual is discarded rather than deferred.
5. Repeating every block for the full week means `reward_per_token_stored` never advances; `harvest_rewards` for any honest staker returns `staker_info.rewards = 0`, even though the pool's reward account holds the full week's funded balance.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-530)
```rust
		#[pallet::call_index(2)]
		pub fn unstake(
			origin: OriginFor<T>,
			pool_id: PoolId,
			amount: T::Balance,
			staker: Option<T::AccountId>,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let staker = staker.unwrap_or(caller.clone());

			// Always start by updating the pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3515-3521)
```rust
		// Equivalent of (current_balance / current_points) * points
		balance(
			u256(current_balance)
				.saturating_mul(u256(points))
				// We check for zero above
				.div(u256(current_points)),
		)
```
