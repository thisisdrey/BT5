### Title
`asset-rewards`: unbounded `stake` calls let any staker permanently zero out reward accrual via `reward_per_token` integer-division truncation - (File: substrate/frame/asset-rewards/src/lib.rs)

### Summary
Sherlock M-9 in Surge shows `getCurrentState()` computing `interest = totalDebt * rate * timeDelta / (365 days * 1e18)`, which truncates to 0 for small `timeDelta`/rate combinations, while the contract still advances `lastAccrueInterestTime`, permanently losing that period's interest. The `pallet-asset-rewards` pallet (`substrate/frame/asset-rewards/src/lib.rs`) contains the same broken invariant: `reward_per_token()` computes `reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked` [1](#0-0)  using a fixed, very small precision constant (`4096`, a `u16`) [2](#0-1) , and `update_pool_rewards` unconditionally advances `last_update_block` to the current block regardless of whether the computed `reward_per_token` increased at all [3](#0-2) .

### Finding Description
`reward_per_token` is the pallet's analogue of Surge's `getCurrentState()`: it derives the pool's accrued-reward-per-share since `last_update_block`:
```
reward_per_token_stored + (reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR) / total_tokens_staked
``` [4](#0-3) 

Unlike `pallet-nomination-pools`, which uses `FixedU128` (10^18 internal precision) for its reward counter and explicitly guards against silently losing zero-value increments (member-level `reward_per_token_paid`/`last_recorded_reward_counter` is only updated `if pending_rewards.is_zero()` is false, see `do_reward_payout`) [5](#0-4) , `pallet-asset-rewards` uses a hard-coded `PRECISION_SCALING_FACTOR = 4096` [2](#0-1)  — roughly 15 orders of magnitude less precision than `FixedU128`. For any pool with a large `total_tokens_staked` (assets commonly use 10–18 decimal places) relative to `reward_rate_per_block * blocks_elapsed`, the integer division rounds to exactly `0`.

Crucially, `update_pool_and_staker_rewards`/`update_pool_rewards` set `pool_info.last_update_block = current_block` and persist `reward_per_token_stored` and the staker's `reward_per_token_paid` unconditionally, with no guard against a zero-delta update [6](#0-5) . This function is invoked on every call to the fully public, unprivileged extrinsics `stake`, `unstake`, and `harvest_rewards` [7](#0-6) [8](#0-7) .

This is functionally identical to the Surge bug: any account (not just the pool admin) can call `stake`/`unstake`/`harvest_rewards` with a trivial or zero-value staking amount (or one's own tokens) every block, which resets the elapsed-block counter used in `reward_per_token` back to a small window each time. If `reward_rate_per_block * elapsed_blocks * 4096 < total_tokens_staked`, every such call recomputes `reward_per_token` as unchanged (`+0`), yet `last_update_block` is bumped to "now" regardless. Because the window is reset every time and the division is a fresh truncation each call, the fractional reward that would have accumulated over a longer un-interrupted interval is never captured — it is discarded on every call rather than deferred and later recovered, unlike nomination-pools where the *member's* claim is skipped but the pool-level accounting is designed with FixedU128 precision specifically to avoid ever needing more than negligible time to reach non-zero (the nomination-pools code has an explicit doc/test, `if_small_member_waits_long_enough_they_will_earn_rewards`, proving that with `FixedU128` precision the reward does eventually surface after enough elapsed blocks even without griefing — the asset-rewards pallet has no equivalent low-precision-safe design, and additionally has no protection against an attacker repeatedly resetting `last_update_block` before the truncation threshold is crossed).

### Impact Explanation
This falls under "runtime bugs that compromise intended behavior" and "public underpriced work that … stalls" reward distribution, per the gate. Stakers who deposited assets into a pool created by an honest admin may receive systematically less than the promised `reward_rate_per_block` emission — for many realistic (asset decimals, reward rate, total stake) combinations, they can receive **zero** rewards indefinitely while a griefer (or simply routine pool activity from many stakers) keeps calling `stake`/`unstake`/`harvest_rewards`, resetting the elapsed-block window before truncation is avoided. The reward tokens that should have flowed to stakers remain locked in the pool account and, when the admin later calls `cleanup_pool` (which requires the pool be empty of stakers but not of reward-asset dust) [9](#0-8) , the entire unspent reward balance is transferred back to the admin — a beneficiary that is not the stakers who should have earned it. This is a fund-loss/incorrect-beneficiary outcome achievable without any privileged or governance action.

### Likelihood Explanation
Likelihood is realistic but conditional on parameters: the truncation reliably occurs whenever `reward_rate_per_block * blocks_elapsed * 4096 < total_tokens_staked`. Given typical ERC20-like 10-18 decimal-precision staking assets and modest `reward_rate_per_block`, this condition is trivially satisfied for single-block windows. Any unprivileged account can call `stake`/`unstake` (even with amount `0` is blocked by `ensure_add_assign`/frozen amount checks, but any account can also call `harvest_rewards` on behalf of any staker each block since `staker: Option<T::AccountId>` allows anyone to trigger it once the pool is expired, or the staker themselves can trigger it every block) to keep resetting the window. Because this doesn't require a malicious relayer, validator, or governance actor — only routine or adversarial use of a public extrinsic — likelihood is Medium-to-High for actively used pools with common configurations.

### Recommendation
- Do not advance `pool_info.last_update_block` (and do not overwrite `staker_info.reward_per_token_paid`) when the freshly computed `reward_per_token` delta is zero — mirror nomination-pools' pattern of skipping the state update on a zero-value accrual, so that elapsed blocks continue to accumulate toward eventually crossing the truncation threshold.
- Increase `PRECISION_SCALING_FACTOR` substantially (e.g. to `10^18`, matching `FixedU128`/nomination-pools' reward-counter precision) or switch `reward_per_token` accounting to a fixed-point type to shrink the truncation window dramatically.
- Add a regression test analogous to nomination-pools' `if_small_member_waits_long_enough_they_will_earn_rewards`/`smallest_claimable_reward` to assert that low reward-rate pools do not permanently lose reward accrual regardless of how often `stake`/`unstake`/`harvest_rewards` are called.

### Proof of Concept
1. `create_pool` with `staked_asset_id` = an 18-decimal asset, `reward_rate_per_block` = e.g. `1_000` (small relative to stake), `expiry_block` far in the future.
2. Staker A stakes `1_000_000 * 10^18` units (`total_tokens_staked` large).
3. Each block, staker A (or any account) calls `harvest_rewards` (or `stake`/`unstake` a trivial extra amount) — this invokes `update_pool_and_staker_rewards` → `reward_per_token()`, computing:
   `reward_rate_per_block (1_000) * blocks_elapsed (1) * PRECISION_SCALING_FACTOR (4096) / total_tokens_staked (1_000_000 * 10^18)` = `0` (integer division truncates).
4. `update_pool_rewards` nonetheless sets `last_update_block = current_block`, so the next call's `blocks_elapsed` resets to `1` again — the reward per token stored never increases.
5. Repeat every block indefinitely: staker A's `reward_per_token_paid` is kept in sync with the always-`0`-incrementing `reward_per_token_stored`, and `staker_info.rewards` never grows despite the pool nominally emitting `1_000` reward tokens/block.
6. Eventually the admin calls `cleanup_pool` once stakers withdraw, and the entire un-distributed reward-asset balance sitting in the pool account is returned to the admin instead of the stakers who were promised it.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L469-502)
```rust
		/// Stake additional tokens in a pool.
		///
		/// A freeze is placed on the staked tokens.
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L569-585)
```rust
		pub fn harvest_rewards(
			origin: OriginFor<T>,
			pool_id: PoolId,
			staker: Option<T::AccountId>,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let staker = staker.unwrap_or(caller.clone());

			// Always start by updating the pool and staker rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info =
				PoolStakers::<T>::get(pool_id, &staker).ok_or(Error::<T>::NonExistentStaker)?;
			let (pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L697-729)
```rust
		pub fn cleanup_pool(origin: OriginFor<T>, pool_id: PoolId) -> DispatchResult {
			let who = ensure_signed(origin)?;

			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			ensure!(pool_info.admin == who, BadOrigin);

			let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
			ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);

			let pool_balance = T::Assets::reducible_balance(
				pool_info.reward_asset_id.clone(),
				&pool_info.account,
				Preservation::Expendable,
				Fortitude::Polite,
			);
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&pool_info.account,
				&pool_info.admin,
				pool_balance,
				Preservation::Expendable,
			)?;

			if let Some((who, cost)) = PoolCost::<T>::take(pool_id) {
				T::Consideration::drop(cost, &who)?;
			}

			Pools::<T>::remove(pool_id);

			Self::deposit_event(Event::PoolCleanedUp { pool_id });

			Ok(())
		}
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3545-3554)
```rust
		// Determine the pending rewards. In scenarios where commission is 100%, `pending_rewards`
		// will be zero.
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}

		// IFF the reward is non-zero alter the member and reward pool info.
		member.last_recorded_reward_counter = current_reward_counter;
		reward_pool.register_claimed_reward(pending_rewards);
```
