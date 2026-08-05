## Title
Griefing via zero-value `stake`/`unstake` permanently advances `last_update_block`, rounding reward-per-token accrual to zero and losing rewards for the accrual period - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
The `pallet-asset-rewards` reward algorithm (`reward_per_token`) computes accrual as `reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked`, then unconditionally stamps `last_update_block = now`, regardless of whether the computed increment rounded to zero. Because `stake` and `unstake` are unprivileged, callable every block, and do not enforce a non-zero amount, an attacker can repeatedly call `stake(pool_id, 0)` on their own account each block to force `rewardable_blocks_elapsed = 1` on every invocation. If `reward_rate_per_block * PRECISION_SCALING_FACTOR < total_tokens_staked`, the integer division truncates to zero, but `last_update_block` still advances past that block, permanently erasing that block's reward accrual for every staker in the pool. This is the direct local analog of the external report's Instance 2 (`_getAccumulatedRewardViaEmissionRate` rounding to zero while `lastAccumulatedTime` still advances).

## Finding Description
`reward_per_token` at [1](#0-0)  computes the new stored reward-per-token value:

```
reward_per_token_stored += reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
```

`PRECISION_SCALING_FACTOR` is a fixed constant of only `4096` [2](#0-1) , far lower precision than Notional's `1e8` `INTERNAL_TOKEN_PRECISION`, making the rounding-to-zero condition easier to hit: it occurs whenever `reward_rate_per_block * blocks_elapsed * 4096 < total_tokens_staked`.

`update_pool_rewards` then unconditionally sets `last_update_block = current_block_number()` even when the computed `reward_per_token` increment was zero [3](#0-2) . This mirrors the vulnerable pattern in `VaultRewarderLib._getAccumulatedRewardViaEmissionRate`/`_accumulateSecondaryRewardViaEmissionRate`, where `lastAccumulatedTime` is advanced regardless of whether the computed increment rounded to zero, permanently losing that period's accrual.

The `stake` extrinsic is fully permissionless for any signed account and does not require `amount > 0`; it calls `update_pool_and_staker_rewards` → `reward_per_token` → `update_pool_rewards` on every invocation, then simply adds `amount` (which can be `0`) to storage [4](#0-3) . `unstake` behaves the same way and also allows `amount = 0` when the caller unstakes from themself [5](#0-4) . There is no guard rejecting a zero `amount`, so an attacker can call `stake(pool_id, 0)` every block at minimal transaction-fee cost (as with Arbitrum/L2-hosted parachains or any low-fee Substrate chain) purely to grief the reward computation, without needing to actually stake or unstake anything.

Because `rewardable_blocks_elapsed` is computed relative to `last_update_block`, forcing an update every single block guarantees `rewardable_blocks_elapsed = 1` for every call, maximizing the chance that the numerator (`reward_rate_per_block * 1 * 4096`) is smaller than `total_tokens_staked` and rounds to zero — after which `last_update_block` is bumped forward and that block's reward is unrecoverably lost to all stakers in the pool.

## Impact Explanation
This is a public, underpriced-work griefing vector directly against a live pallet's reward accounting (`pallet-asset-rewards`), matching the "public underpriced work that degrades... reward payouts" impact category. As `total_tokens_staked` grows (higher TVL pools are more attractive griefing targets, same dynamic noted in the original report), the window in which the increment rounds to zero widens, and an attacker can indefinitely suppress reward accrual for legitimate stakers in the pool at near-zero cost to themselves, with no benefit for the attacker other than causing continuous loss for other users — a pure griefing/DoS-on-rewards attack with a definite, unrecoverable loss of funds (foregone reward-asset payouts) and no external condition beyond signing zero-value transactions.

## Likelihood Explanation
The attack requires no privileged role, no governance, no malicious validator/collator/relayer, and no leaked keys — only an ordinary signed account calling a public extrinsic (`stake` or `unstake`) with a zero amount every block. On any chain with low transaction fees this is trivially repeatable and cheap, exactly the condition highlighted as high-risk in the source report (L2/low-fee environments). The precision constant (`4096`) here is smaller than Notional's `1e8`, making the rounding-to-zero condition proportionally easier to trigger for realistic `reward_rate_per_block`/`total_tokens_staked` ratios.

## Recommendation
- Reject zero-amount `stake`/`unstake` calls (`ensure!(!amount.is_zero(), ...)`), removing the cheapest griefing vector.
- More fundamentally, do not advance `last_update_block` when the computed reward-per-token increment is zero; only advance it up to the last block for which a non-zero (or otherwise correctly rounded) increment was actually accounted, or accumulate a remainder/dust tracker so unrounded reward-time is not silently discarded.
- Consider increasing `PRECISION_SCALING_FACTOR` or switching to a higher-precision fixed-point type (e.g., `FixedU128`) for `reward_per_token_stored`, analogous to how `pallet-nomination-pools` uses an always-cumulative, high-precision `FixedU128` reward counter (`RewardCounter`) that is far more resistant to per-block-griefing rounding loss [6](#0-5) .

## Proof of Concept
1. Admin creates a pool via `create_pool` with `reward_rate_per_block = R` and stakers already holding `total_tokens_staked = S`, chosen (or naturally reached via TVL growth) such that `R * 4096 < S`.
2. Attacker (any signed account, does not even need existing stake) calls `stake(pool_id, 0)` in every subsequent block.
3. Each call triggers `update_pool_and_staker_rewards` → `reward_per_token`, computing `rewardable_blocks_elapsed = 1`, and `R * 1 * 4096 / S == 0` due to integer truncation [7](#0-6) .
4. `update_pool_rewards` still sets `last_update_block = now` [8](#0-7) , discarding that block's reward permanently — subsequent legitimate `harvest_rewards` calls by real stakers will never see that block's `reward_rate_per_block` accounted in `reward_per_token_stored`.
5. Repeating step 2 every block indefinitely suppresses essentially all reward accrual for the pool as long as `S` stays large enough relative to `R`, at a cost of only gas for zero-value `stake` calls.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-560)
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

			// Check the staker has enough staked tokens.
			ensure!(staker_info.amount >= amount, Error::<T>::NotEnoughTokens);

			// Unfreeze staker assets.
			T::AssetsFreezer::decrease_frozen(
				pool_info.staked_asset_id.clone(),
				&FreezeReason::Staked.into(),
				&staker,
				amount,
			)?;

			// Update Pools.
			pool_info.total_tokens_staked.ensure_sub_assign(amount)?;
			Pools::<T>::insert(pool_id, pool_info);

			// Update PoolStakers.
			staker_info.amount.ensure_sub_assign(amount)?;

			if staker_info.amount.is_zero() && staker_info.rewards.is_zero() {
				PoolStakers::<T>::remove(&pool_id, &staker);
			} else {
				PoolStakers::<T>::insert(&pool_id, &staker, staker_info);
			}

			// Emit event.
			Self::deposit_event(Event::Unstaked { caller, staker, pool_id, amount });

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L786-809)
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1506-1509)
```rust
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;
```
