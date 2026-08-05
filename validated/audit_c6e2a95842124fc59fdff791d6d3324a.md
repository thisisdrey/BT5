Audit Report

## Title
Reward-per-token accrual overflow permanently freezes staked assets and rewards in `pallet-asset-rewards` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`Pallet::reward_per_token` computes `reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR` in `T::Balance`-typed checked arithmetic before dividing by `total_tokens_staked`. Because `staked_asset_id` and `reward_asset_id` are independent `fungibles` assets that can carry arbitrary/mismatched decimal precision, this intermediate product can exceed `T::Balance::MAX` and hard-error via `ensure_mul`, and since this computation runs unconditionally at the top of `stake`, `unstake`, and `harvest_rewards`, the overflow permanently blocks all further interaction with the pool — including `unstake`, the only path to release frozen principal.

## Finding Description
`reward_per_token` performs the multiplication chain `reward_rate_per_block.ensure_mul(rewardable_blocks_elapsed.into())?.ensure_mul(PRECISION_SCALING_FACTOR.into())?.ensure_div(total_tokens_staked)?` [1](#0-0)  using the fixed constant `PRECISION_SCALING_FACTOR = 4096` [2](#0-1) . This function is invoked unconditionally as the first step of `update_pool_and_staker_rewards` [3](#0-2) , which in turn is called before the actual state-mutating work in `stake` (before `increase_frozen`) [4](#0-3) , in `unstake` (before `decrease_frozen`, the only unfreeze path for staked principal) [5](#0-4) , and in `harvest_rewards` (before the reward `transfer`) [6](#0-5) . Once the intermediate multiplication overflows `T::Balance`, `ensure_mul` returns `Err(ArithmeticError::Overflow)`, causing every subsequent call touching that pool to revert. `cleanup_pool` cannot recover the pool because it requires `PoolStakers::iter_key_prefix(pool_id).next().is_none()` [7](#0-6) , which can never be satisfied once `unstake` is permanently broken. There is no normalization of `reward_rate_per_block`/`PRECISION_SCALING_FACTOR` against the decimal precision of the two independently chosen assets anywhere in the accrual path, unlike the analogous `U256`-widened ratio math used in `nomination-pools::balance_to_point` [8](#0-7) .

## Impact Explanation
Once the overflow condition is hit for a given `pool_id`, all current stakers of that pool are permanently unable to `unstake` their frozen principal or `harvest_rewards`, and no admin extrinsic can recover the pool state (`cleanup_pool` is blocked by the non-empty-stakers guard). This is a permanent user-fund lock, an impact explicitly in-scope under the Polkadot SDK Impact Gate.

## Likelihood Explanation
Reaching the overflow requires only that `total_elapsed_blocks` grow (which happens automatically every block up to `expiry_block`) against a `reward_rate_per_block` sized for the reward asset's decimals; no malicious action, governance abuse, or attacker-controlled trigger beyond ordinary calls to public `stake`/`unstake`/`harvest_rewards` extrinsics is needed once a pool with mismatched-decimal assets and a plausible reward rate exists. The unprivileged stakers who fall victim to the freeze interact only through the pallet's normal, public extrinsics; the bug is a missing-overflow-safety defect in the accrual arithmetic itself, not a privileged-actor exploit.

## Recommendation
Perform the `reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR` scaling multiplication in a wider intermediate type (e.g., `U256`, mirroring `nomination-pools::balance_to_point`/`point_to_balance`) and only narrow back to `T::Balance` after the final division, so the intermediate product cannot exceed `T::Balance::MAX` regardless of the staked/reward asset decimal mismatch. Additionally, add a recovery extrinsic (e.g., a root/governance-gated pool reset or forced unfreeze) that does not depend on `reward_per_token` succeeding, so pools cannot become permanently unrecoverable.

## Proof of Concept
1. Create a pool via `create_pool` with a low-decimal `staked_asset_id`, a high-decimal `reward_asset_id`, a `reward_rate_per_block` sized for the reward asset's decimals, and a long-lived `expiry_block`.
2. Call `stake` to deposit tokens while `rewardable_blocks_elapsed` is small (succeeds).
3. Advance blocks until `reward_rate_per_block.ensure_mul(rewardable_blocks_elapsed)?.ensure_mul(4096)` exceeds `T::Balance::MAX`, as computed in `reward_per_token` [9](#0-8) .
4. Call `unstake` or `harvest_rewards` — `reward_per_token` returns `Err(ArithmeticError::Overflow)`, the extrinsic fails, and the staker's frozen tokens and accrued rewards remain permanently locked since `cleanup_pool` requires no remaining stakers [10](#0-9) .

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L473-491)
```rust
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

```

**File:** substrate/frame/asset-rewards/src/lib.rs (L514-540)
```rust
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
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L569-595)
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

			// Transfer unclaimed rewards from the pool to the staker.
			T::Assets::transfer(
				pool_info.reward_asset_id,
				&pool_info.account,
				&staker,
				staker_info.rewards,
				// Could kill the account, but only if the pool was already almost empty.
				Preservation::Expendable,
			)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L697-704)
```rust
		pub fn cleanup_pool(origin: OriginFor<T>, pool_id: PoolId) -> DispatchResult {
			let who = ensure_signed(origin)?;

			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			ensure!(pool_info.admin == who, BadOrigin);

			let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
			ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3475-3499)
```rust
	fn balance_to_point(
		current_balance: BalanceOf<T>,
		current_points: BalanceOf<T>,
		new_funds: BalanceOf<T>,
	) -> BalanceOf<T> {
		let u256 = T::BalanceToU256::convert;
		let balance = T::U256ToBalance::convert;
		match (current_balance.is_zero(), current_points.is_zero()) {
			(_, true) => new_funds.saturating_mul(POINTS_TO_BALANCE_INIT_RATIO.into()),
			(true, false) => {
				// The pool was totally slashed.
				// This is the equivalent of `(current_points / 1) * new_funds`.
				new_funds.saturating_mul(current_points)
			},
			(false, false) => {
				// Equivalent to (current_points / current_balance) * new_funds
				balance(
					u256(current_points)
						.saturating_mul(u256(new_funds))
						// We check for zero above
						.div(u256(current_balance)),
				)
			},
		}
	}
```
