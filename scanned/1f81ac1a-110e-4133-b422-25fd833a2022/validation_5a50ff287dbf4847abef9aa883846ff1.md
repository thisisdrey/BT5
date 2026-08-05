## Analysis

The Solidity report's core broken invariant is: an *unbounded intermediate value* (reward-rate × elapsed-time × precision-scaling-factor) is computed before being divided down and stored, and when the staked/liquidity token has few decimals relative to the reward token, that intermediate value overflows and the transaction reverts — permanently freezing both the reward payout and (transitively) the user's staked capital, since every user-facing entrypoint that touches the pool must first "catch up" the reward accounting.

The `pallet-asset-rewards` (Synthetix-style `StakingRewards` port) reproduces this exact pattern in Substrate. `Pallet::reward_per_token` computes: [1](#0-0) 

using `PRECISION_SCALING_FACTOR = 4096` as the scaling constant: [2](#0-1) 

`reward_rate_per_block` and `total_tokens_staked` are both denominated in `T::Balance` for two **independent, admin-chosen `fungibles` asset IDs** — the staked asset and the reward asset can have arbitrary/different decimal precisions (e.g. a 6-decimal staked LP-style asset vs. an 18-decimal reward asset), exactly the decimal-mismatch precondition from the Solidity report.

`reward_per_token` is invoked from every state-mutating entrypoint via `update_pool_and_staker_rewards`, and this call happens **before** the actual operation (freeze increase/decrease or reward transfer): [3](#0-2) 

- `stake`: `Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;` before increasing the freeze [4](#0-3) 
- `unstake`: same call before `decrease_frozen`, i.e. before a staker can recover their principal [5](#0-4) 
- `harvest_rewards`: same call before transferring rewards [6](#0-5) 

The multiplication chain uses `ensure_mul`/`ensure_div` (checked arithmetic that returns `Err(ArithmeticError::Overflow)` rather than saturating), so once `reward_rate_per_block.ensure_mul(blocks_elapsed)?.ensure_mul(4096)?` exceeds `T::Balance::MAX` (typically `u128::MAX`), the call errors out and the whole extrinsic reverts.

Because `reward_per_token` is called unconditionally as the very first step of `stake`, `unstake`, and `harvest_rewards`, once the overflow condition is reached for a pool, **every subsequent call into that pool by any staker fails**, including `unstake`, which is the only path to release the `AssetsFreezer` freeze placed on the staker's principal. This is not an admin-abuse scenario — the admin only sets normal-looking, valid parameters (`reward_rate_per_block` for an 18-decimal reward asset, paired with a low-decimal staked asset), and ordinary block-by-block accrual (`rewardable_blocks_elapsed` growing every block until pool expiry) is what drives the overflow; no malicious actor, governance abuse, or off-chain assumption is required.

### Why existing guards don't stop this
- `ensure_mul`/`ensure_div` prevent silent wraparound, but converting overflow into a hard revert on the reward-accrual read path means the failure mode is "permanently stuck pool", not "graceful degrade" — since there is no code path to update the pool state, unfreeze stake, or drain the pool without first computing `reward_per_token`.
- There's no cap or normalization between `reward_rate_per_block`, `PRECISION_SCALING_FACTOR`, and the decimal precision of `staked_asset_id` vs `reward_asset_id`, so nothing prevents an admin from configuring (even unintentionally) a pool whose parameters are individually valid but whose accrual arithmetic overflows well before `expiry_block`.
- `cleanup_pool` requires `stakers.is_none()` (no `PoolStakers` entries), which can never be satisfied once `unstake` is permanently broken for that pool — so there is no admin recovery path once the overflow condition is hit. [7](#0-6) 

### Recommendation
Perform the scaling multiplication in a wider intermediate type (e.g. `U256` as already used elsewhere in this codebase for analogous ratio math, such as `nomination-pools`'s `balance_to_point`/`point_to_balance`) and only narrow back to `T::Balance` after the division, or normalize `PRECISION_SCALING_FACTOR`/reward-rate scale relative to the staked/reward asset decimal difference so intermediate values can't exceed `T::Balance::MAX`. See the U256-based pattern already used for comparable safety in nomination-pools: [8](#0-7) 

### Title
Reward-per-token accrual overflow permanently freezes staked assets and rewards in `pallet-asset-rewards` - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`Pallet::reward_per_token` computes `reward_rate_per_block * blocks_elapsed * PRECISION_SCALING_FACTOR` via checked (`ensure_mul`) arithmetic before dividing by `total_tokens_staked`. Because the staked and reward assets are independent `fungibles` assets that can have very different decimal precision, this intermediate product can overflow `T::Balance`, causing a hard error. Since `reward_per_token` (through `update_pool_and_staker_rewards`) is called unconditionally at the start of `stake`, `unstake`, and `harvest_rewards`, an overflow permanently blocks all three operations for that pool — locking both staked principal (still frozen via `AssetsFreezer`) and unclaimed rewards, with no recovery extrinsic.

### Finding Description
See analysis above; relevant code: `reward_per_token` [9](#0-8) , and the three entrypoints that depend on it succeeding before performing their real work [10](#0-9) [11](#0-10) [12](#0-11) .

### Impact Explanation
Once the arithmetic overflows for a given `pool_id`, no staker can `unstake` (freeze release), `harvest_rewards` (reward transfer), or `stake` more into that pool — a permanent, protocol-level fund/state lock affecting all current stakers of the pool, satisfying the "permanent user-fund lock" impact category.

### Likelihood Explanation
Pool admins freely choose `staked_asset_id`, `reward_asset_id`, and `reward_rate_per_block` for arbitrary `fungibles` assets with independent decimal precision; over the life of a pool `rewardable_blocks_elapsed` (bounded only by `expiry_block - last_update_block`) grows every block, so for any pool where `reward_rate_per_block * total_elapsed_blocks * 4096` can approach `T::Balance::MAX`, the failure is a deterministic function of time/config, not of any attacker action — no malicious peer, admin abuse, or privileged action is required to trigger it, only ordinary passage of blocks under a plausible parameter choice.

### Recommendation
Widen the intermediate scaling multiplication to a bigger integer type (`U256`) as already done in `nomination-pools::balance_to_point`/`point_to_balance`, and/or bound `reward_rate_per_block` relative to `total_tokens_staked`/decimals at pool-creation/rate-update time so the accrual arithmetic can never overflow `T::Balance` during the pool's configured lifetime. Additionally, add a recovery path (e.g. a root/governance force-unfreeze or pool-reset extrinsic) that does not depend on `reward_per_token` succeeding.

### Proof of Concept
1. `create_pool` with `staked_asset_id` = an asset with low total supply/decimals (e.g. total staked ends up as a small `T::Balance` value) and `reward_asset_id` with a large `reward_rate_per_block` sized for high-decimal rewards, and a long-lived `expiry_block`.
2. `stake` some tokens (succeeds while `rewardable_blocks_elapsed` is small).
3. Advance blocks until `reward_rate_per_block.ensure_mul(rewardable_blocks_elapsed)?.ensure_mul(4096)` exceeds `T::Balance::MAX` (guaranteed eventually given a fixed nonzero `reward_rate_per_block` and growing `blocks_elapsed`), as computed in [13](#0-12) .
4. Call `unstake` or `harvest_rewards` — `reward_per_token` returns `Err(ArithmeticError::Overflow)`, the extrinsic fails, and the staker's frozen tokens and accrued rewards become permanently inaccessible since `cleanup_pool` requires no remaining stakers [14](#0-13) .

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L117-118)
```rust
/// Multiplier to maintain precision when calculating rewards.
pub(crate) const PRECISION_SCALING_FACTOR: u16 = 4096;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L473-502)
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

			Pools::<T>::insert(pool_id, pool_info);

			// Update PoolStakers.
			staker_info.amount.ensure_add_assign(amount)?;
			PoolStakers::<T>::insert(pool_id, &staker, staker_info);

			// Emit event.
			Self::deposit_event(Event::Staked { staker, pool_id, amount });

			Ok(())
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L514-560)
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L569-615)
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

			// Emit event.
			Self::deposit_event(Event::RewardsHarvested {
				caller,
				staker: staker.clone(),
				pool_id,
				amount: staker_info.rewards,
			});

			// Reset staker rewards.
			staker_info.rewards = 0u32.into();

			if staker_info.amount.is_zero() {
				PoolStakers::<T>::remove(&pool_id, &staker);
			} else {
				PoolStakers::<T>::insert(&pool_id, &staker, staker_info);
			}

			Ok(())
		}
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
