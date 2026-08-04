Based on the investigation, the strongest local analog to the reported "shared reward-accounting state can be permanently poisoned by an ordinary user, locking rewards for everyone" bug class is found in `pallet-asset-rewards`, which computes a single pool-wide `reward_per_token_stored` value that every staker's harvest/stake/unstake call depends on and updates atomically.

### Title
Attacker-controlled `total_tokens_staked` shrinkage can permanently poison pool-wide `reward_per_token_stored`, locking all stakers' rewards - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes rewards for every staker from a single shared pool value, `PoolInfo::reward_per_token_stored` [1](#0-0) , which is recomputed on every `stake`, `unstake`, and `harvest_rewards` call via `reward_per_token()` [2](#0-1) . The formula divides `reward_rate_per_block * elapsed_blocks * PRECISION_SCALING_FACTOR` by `pool_info.total_tokens_staked`, and any staker can unilaterally shrink `total_tokens_staked` toward its minimum via `unstake`, since unstaking is permissionless for the caller's own stake [3](#0-2) .

### Finding Description
Similar to the `GiantMevAndFeesPool`/`GiantLP` bug, where an unprotected shared hook path let any user corrupt the reward bookkeeping used by everyone, `pallet-asset-rewards` centralizes reward math in one pool-level counter that every participant's operation must successfully recompute using `ensure_mul`/`ensure_add`/`ensure_div` (checked arithmetic) [4](#0-3) . Because `total_tokens_staked` is fully attacker-influenced (any staker can reduce it toward the smallest non-zero remainder by unstaking), and the numerator grows unbounded with elapsed blocks and the pool's reward rate, an attacker can drive the per-block increment to `reward_per_token_stored` to be extremely large relative to `total_tokens_staked`. Once enough blocks elapse with a tiny `total_tokens_staked`, the `ensure_mul`/`ensure_add` chain in `reward_per_token()` will overflow `T::Balance` and return an `ArithmeticError`, which propagates out of `update_pool_and_staker_rewards` [5](#0-4)  and is used unconditionally as the first step of `stake`, `unstake`, and `harvest_rewards` [6](#0-5) [7](#0-6) [8](#0-7) . Since this recomputation is deterministic and re-derived from persistent on-chain state (`last_update_block`, `reward_rate_per_block`, `total_tokens_staked`) every single call, once the overflow condition is reached it recurs on every future block indefinitely — there is no way to reset `reward_per_token_stored` or `last_update_block` without first calling one of the same three entry points, all of which fail identically.

### Impact Explanation
This permanently locks the reward asset already deposited into the pool's account (see `deposit_reward_tokens`, which places tokens under the pool's control [9](#0-8) ) and prevents *all* stakers — not just the attacker — from ever calling `harvest_rewards` or `unstake` again, since both paths require `update_pool_and_staker_rewards` to succeed first. Staked assets remain frozen via `T::AssetsFreezer` [10](#0-9)  and become unrecoverable because the only unfreeze path (`unstake`) is also blocked. This matches the "permanent user-fund or bridge-state lock" and "reward payout state must only advance after ... settlement succeed atomically" impact classes, since the shared payout ledger is corrupted for every participant by one attacker's unprivileged actions.

### Likelihood Explanation
Triggering requires only an ordinary signed account able to call `stake`/`unstake` on an existing pool (no admin, governance, or privileged origin needed — `stake`/`unstake`/`harvest_rewards` are open dispatchables) [11](#0-10) [12](#0-11) . The attacker needs to reduce `total_tokens_staked` to a very small value (e.g., by being the dominant staker and unstaking most of their position) and let enough blocks pass for the numerator to overflow; the exact block count needed depends on the pool's configured `reward_rate_per_block`, which is chosen by the pool admin and can be large. This is data-dependent rather than universally guaranteed on every pool, so likelihood is moderate rather than certain, but it requires no privileged capability and is fully within reach of a single unprivileged staker for a pool with unfavorable (admin-set, but not attacker-controlled) parameters.

### Recommendation
- Bound the growth of `reward_per_token_stored` independent of `total_tokens_staked`, e.g. enforce a sane minimum stake before allowing further reward accrual, or clamp/cap accrual per update instead of using unbounded `ensure_mul` on unelapsed-block products.
- Consider using saturating arithmetic combined with a fallback "pause pool" path that lets an admin migrate/reset the reward counter without requiring stakers to be unstuck through the same poisoned computation.
- Add a floor on `total_tokens_staked` in `reward_per_token()` (e.g., skip/no-op accrual when total staked is negligible/dust) to prevent division-driven blow-up of the counter from a single dominant staker's unstake action.

### Proof of Concept
1. Admin creates a pool with a nontrivial `reward_rate_per_block` and a distant `expiry_block`.
2. Attacker (or a large staker) calls `stake` to become the sole/dominant staker with a large `amount`, then calls `unstake` repeatedly to bring `total_tokens_staked` down to `1` (the minimum non-zero value), each call going through `update_pool_and_staker_rewards` successfully while `total_tokens_staked` is still large enough to avoid overflow.
3. Time passes (many blocks elapse) with `total_tokens_staked == 1`.
4. Any staker calls `harvest_rewards`, `stake`, or `unstake`; `reward_per_token()` computes `reward_rate_per_block * elapsed_blocks * 4096 / 1`, which for a large enough elapsed-block count overflows `T::Balance` in the `ensure_mul` chain, returning `Error::<T>::...` (`ArithmeticError`) via `?`.
5. From this point on, every future call to `stake`, `unstake`, and `harvest_rewards` on this pool recomputes the same overflow (since `last_update_block` never advances on a failed/reverted extrinsic), permanently locking all stakers' funds and unclaimed rewards in the pool.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L161-164)
```rust
	/// Total rewards accumulated per token, up to the `last_update_block`.
	reward_per_token_stored: Balance,
	/// Last block number the pool was updated.
	last_update_block: BlockNumber,
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L472-474)
```rust
		#[pallet::call_index(1)]
		pub fn stake(origin: OriginFor<T>, pool_id: PoolId, amount: T::Balance) -> DispatchResult {
			let staker = ensure_signed(origin)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L476-480)
```rust
			// Always start by updating staker and pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L482-487)
```rust
			T::AssetsFreezer::increase_frozen(
				pool_info.staked_asset_id.clone(),
				&FreezeReason::Staked.into(),
				&staker,
				amount,
			)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-545)
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
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L582-585)
```rust
			let staker_info =
				PoolStakers::<T>::get(pool_id, &staker).ok_or(Error::<T>::NonExistentStaker)?;
			let (pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L672-688)
```rust
		#[pallet::call_index(7)]
		pub fn deposit_reward_tokens(
			origin: OriginFor<T>,
			pool_id: PoolId,
			amount: T::Balance,
		) -> DispatchResult {
			let caller = ensure_signed(origin)?;
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&caller,
				&pool_info.account,
				amount,
				Preservation::Preserve,
			)?;
			Ok(())
		}
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
