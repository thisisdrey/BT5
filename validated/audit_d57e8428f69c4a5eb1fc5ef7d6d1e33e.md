Audit Report

## Title
Reward-per-token griefing via low `PRECISION_SCALING_FACTOR` permanently zeroes staker rewards in `pallet-asset-rewards` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`pallet-asset-rewards`'s `reward_per_token` function computes `reward_per_token_stored + reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked` using a fixed multiplier of only `4096`, and `update_pool_rewards` unconditionally advances `last_update_block` and overwrites `reward_per_token_stored` with this result even when the increment integer-divides to zero. Since `stake`, `unstake`, and `harvest_rewards` are permissionless and always call `update_pool_and_staker_rewards` first, any account can repeatedly trigger this update with small `blocks_elapsed`, causing that period's reward accrual to be silently and permanently dropped for all stakers in the pool.

## Finding Description
`reward_per_token` at [1](#0-0)  computes the increment via `reward_rate_per_block.ensure_mul(blocks_elapsed).ensure_mul(PRECISION_SCALING_FACTOR).ensure_div(total_tokens_staked)`, where `PRECISION_SCALING_FACTOR` is a constant `4096` [2](#0-1) . This is far smaller than the `1e18`/`U256`-scale precision nomination-pools uses for equivalent point-to-balance conversions [3](#0-2) .

`update_pool_rewards` unconditionally sets `new_pool_info.last_update_block = current_block_number()` and `new_pool_info.reward_per_token_stored = reward_per_token`, regardless of whether that value changed from the previous stored value due to integer-division rounding to zero [4](#0-3) . There is no carryover or dust-tracking mechanism, so once `last_update_block` advances past a period whose increment rounded to zero, that period's rewards are permanently unrecoverable.

`update_pool_and_staker_rewards`, which calls `reward_per_token` and `update_pool_rewards`, is invoked unconditionally at the start of every `stake` and `unstake` call [5](#0-4) , and `stake` is a permissionless, signed-origin-only extrinsic with no minimum amount enforced beyond normal freeze semantics [6](#0-5) . `unstake` is similarly callable by the staker at will [7](#0-6) .

Given a pool where `total_tokens_staked` is large relative to `reward_rate_per_block * 4096`, calling `stake`/`unstake` every block keeps `blocks_elapsed = 1` each time, making the increment round to zero on every call while `last_update_block` still advances, permanently discarding accrued rewards for every staker in the pool — not just the caller.

## Impact Explanation
This causes permanent loss of reward accrual for all stakers in an affected pool despite the pool being funded with reward tokens, matching the "permanent user-fund ... lock" impact class in the accepted impact gate, since funded reward tokens become practically unrecoverable by ordinary stakers once `reward_per_token_stored` stalls.

## Likelihood Explanation
Any signed account can call `stake`/`unstake` on a pool every block at minimal cost (transaction fee plus a trivial freeze amount), and the precondition — `total_tokens_staked` large relative to `reward_rate_per_block * PRECISION_SCALING_FACTOR` — is a normal configuration for pools with lower-decimal or lower-rate reward assets staked against high-decimal staked assets, requiring no privileged access or unusual conditions.

## Recommendation
Increase `PRECISION_SCALING_FACTOR` to at least `10^18`-scale precision (or use `U256` intermediate arithmetic as done in nomination-pools' `point_to_balance`) before dividing by `total_tokens_staked`, and consider tracking undistributed remainder/dust across updates instead of dropping it whenever `reward_per_token` rounds down between calls.

## Proof of Concept
1. Create a pool with an 18-decimal staked asset and a reward asset/rate such that `reward_rate_per_block * 4096` is small relative to `total_tokens_staked` (e.g., `total_tokens_staked = 1_000_000e18`, `reward_rate_per_block ≈ 12000`).
2. Have an attacker account call `stake(pool_id, 1)` every block.
3. Observe via `reward_per_token()` at [1](#0-0)  that the computed increment integer-divides to `0` each call.
4. Observe `update_pool_rewards` at [8](#0-7)  still advances `last_update_block`, so `reward_per_token_stored` never increases.
5. Confirm that `harvest_rewards` for an honest staker who never interacts returns `0` accrued rewards despite the pool's reward account holding the full period's funded balance.

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
