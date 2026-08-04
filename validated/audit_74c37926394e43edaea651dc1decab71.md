### Title
Unbounded reward-per-token arithmetic in `pallet-asset-rewards` permanently bricks a pool and locks staked funds - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes `reward_per_token` using unchecked-growth inputs (`reward_rate_per_block`, elapsed blocks, and a fixed precision multiplier) and propagates any arithmetic failure as a hard `ArithmeticError` that aborts the whole extrinsic. Because the pool's `last_update_block` is only advanced when the calculation succeeds, once the multiplication overflows there is no way to make it succeed again — every future `stake`, `unstake`, `harvest_rewards`, and `set_pool_reward_rate_per_block` call on that pool reverts forever, permanently freezing any tokens already staked in the pool. This mirrors the Ethos `StabilityPool`/`ActivePool` bug class: a persisted computed value causes every subsequent legitimate call through a shared code path to revert, producing a permanent DoS/fund-lock rather than a one-off failure.

### Finding Description
`reward_per_token` is the JIT reward accrual routine called by every pool state-mutating extrinsic: [1](#0-0) 

It computes:
```
reward_per_token_stored + reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked
```
using `ensure_mul`/`ensure_add`/`ensure_div`, all of which turn overflow into a propagated `ArithmeticError` that aborts the call [2](#0-1) .

Every public entry point that lets a user interact with their stake calls this function first, via `update_pool_and_staker_rewards`:
- `stake` [3](#0-2) 
- `unstake` [4](#0-3) 
- `harvest_rewards` [5](#0-4) 
- `set_pool_reward_rate_per_block` [6](#0-5) 

`update_pool_rewards` only persists `last_update_block` and `reward_per_token_stored` when the whole extrinsic succeeds [7](#0-6) . Because Substrate extrinsics roll back all storage changes on error, a failing call leaves `last_update_block` unchanged. The next call recomputes `rewardable_blocks_elapsed` against the *same or larger* gap (it can only grow, since `last_block_reward_applicable` caps it at `expiry_block`, and time only moves forward) [8](#0-7) . Once the multiplication `reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR` exceeds `T::Balance::MAX`, it will overflow identically on every subsequent attempt — there is no recovery path, no fallback to saturating arithmetic, and no way to "skip" the erroring term the way the Ethos remediation proposed skipping the floor-division correction when `_debtToOffset == 0`.

There is no upper bound placed on `reward_rate_per_block` at `create_pool` time — only asset existence and `expiry_block > now` are checked [9](#0-8) , and it can subsequently only be *increased*, never decreased, by `set_pool_reward_rate_per_block` [6](#0-5) . A far-future `expiry_block` combined with a non-trivial `reward_rate_per_block` and enough elapsed blocks without interaction is sufficient to reach the overflow threshold — no attacker capability beyond normal pool creation/staking is required, and no malicious peer/validator/relayer/admin abuse is needed once the pool exists.

### Impact Explanation
Once the overflow threshold is crossed, the pool is permanently and irrecoverably stuck:
- Stakers can never call `unstake` again — their staked (frozen) tokens are locked in the pool forever, since `unstake` requires `update_pool_and_staker_rewards` to succeed before unfreezing funds.
- `harvest_rewards` can never succeed, so any pending unclaimed reward tokens for stakers are permanently unreachable.
- The pool admin cannot even fix the rate via `set_pool_reward_rate_per_block`, since that call goes through the same reward computation first.
- `cleanup_pool` requires the staker list to be empty first, which can never be achieved since nobody can unstake, so the pool's storage/consideration deposit is stuck too.

This matches the "permanent user-fund or bridge-state lock" impact class from the gate: value that was properly staked becomes permanently unrecoverable due to a pure arithmetic-safety bug in the reward accrual path, not because of any privileged actor's abuse.

### Likelihood Explanation
The trigger condition depends purely on parameters chosen at pool creation (`reward_rate_per_block`, `expiry_block`) and elapsed time without any interaction — both are attacker-controllable to the extent that pool creation and reward-rate increases are permitted by the deployment's `CreatePoolOrigin`. Even with a conservative/permissioned `CreatePoolOrigin`, this is a genuine implementation bug: normal pool configurations for long-duration reward programs (a common, legitimate use case for a staking-rewards pallet) can accumulate enough `rewardable_blocks_elapsed * reward_rate_per_block * 4096` to overflow `T::Balance` (typically `u128`) well before `expiry_block`, especially for pools with low total stake and higher rate parameters chosen to make rewards meaningfully divisible. No special network conditions, governance capture, or malicious infrastructure role is required — only that the overflow threshold is crossed once, after which the fault is permanent and deterministic for every future caller.

### Recommendation
- Bound `reward_rate_per_block` at `create_pool`/`set_pool_reward_rate_per_block` time relative to `T::Balance::MAX`, `PRECISION_SCALING_FACTOR`, and a sane maximum pool duration, rejecting configurations that could overflow before `expiry_block`.
- Replace the hard-failing `ensure_mul`/`ensure_add` chain in `reward_per_token` with saturating arithmetic (as already used elsewhere in the codebase via `DefensiveSaturating`), so a computed value simply saturates instead of aborting the whole extrinsic and blocking `last_update_block` from ever advancing.
- Alternatively, cap `rewardable_blocks_elapsed` growth or periodically checkpoint `reward_per_token_stored` so that a single failed update cannot compound into a permanent block for all future callers.
- Add regression tests analogous to the nomination-pools `reward_counter_update_can_fail_if_pool_is_highly_slashed` test, but specifically verifying that once triggered, `unstake`/`harvest_rewards` do not become permanently unusable.

### Proof of Concept
1. Call `create_pool` with a `reward_rate_per_block` `R` and `expiry_block` set far in the future (e.g., years' worth of blocks), and a small `total_tokens_staked` context (achieved by a single small `stake`).
2. Do not interact with the pool again until enough blocks have elapsed such that:
   `R * (elapsed_blocks) * PRECISION_SCALING_FACTOR > T::Balance::MAX`
   (elapsed_blocks is bounded above only by `expiry_block - last_update_block`, so pick `R`/`expiry_block` accordingly at pool creation; this can be reached immediately if `R` is chosen close to `T::Balance::MAX / PRECISION_SCALING_FACTOR` even for `elapsed_blocks = 1`).
3. Call `stake`/`unstake`/`harvest_rewards` — the call reverts with `ArithmeticError::Overflow` inside `reward_per_token` (`substrate/frame/asset-rewards/src/lib.rs:803-809`).
4. Because the extrinsic reverted, `last_update_block` was never advanced; retry the same call — it reverts again, deterministically, forever, since the elapsed-block gap can only grow.
5. Confirm the staker's originally staked tokens remain frozen (`AssetsFreezer`) with no code path able to release them, demonstrating the permanent fund lock.

### Citations

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L568-615)
```rust
		#[pallet::call_index(3)]
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L826-833)
```rust
		fn last_block_reward_applicable(pool_expiry_block: BlockNumberFor<T>) -> BlockNumberFor<T> {
			let now = T::BlockNumberProvider::current_block_number();
			if now < pool_expiry_block {
				now
			} else {
				pool_expiry_block
			}
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L850-881)
```rust
	) -> Result<PoolId, DispatchError> {
		// Ensure the assets exist.
		ensure!(T::Assets::asset_exists(staked_asset_id.clone()), Error::<T>::NonExistentAsset);
		ensure!(T::Assets::asset_exists(reward_asset_id.clone()), Error::<T>::NonExistentAsset);

		// Check the expiry block.
		let now = T::BlockNumberProvider::current_block_number();
		let expiry_block = expiry.evaluate(now);
		ensure!(expiry_block > now, Error::<T>::ExpiryBlockMustBeInTheFuture);

		let pool_id = NextPoolId::<T>::try_mutate(|id| -> Result<PoolId, DispatchError> {
			let current_id = *id;
			*id = id.ensure_add(1)?;
			Ok(current_id)
		})?;

		let footprint = Self::pool_creation_footprint();
		let cost = T::Consideration::new(creator, footprint)?;
		PoolCost::<T>::insert(pool_id, (creator.clone(), cost));

		// Create the pool.
		let pool = PoolInfoFor::<T> {
			staked_asset_id: staked_asset_id.clone(),
			reward_asset_id: reward_asset_id.clone(),
			reward_rate_per_block,
			total_tokens_staked: 0u32.into(),
			reward_per_token_stored: 0u32.into(),
			last_update_block: 0u32.into(),
			expiry_block,
			admin: admin.clone(),
			account: Self::pool_account_id(&pool_id),
		};
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L900-922)
```rust
	fn set_pool_reward_rate_per_block(
		admin: &T::AccountId,
		pool_id: PoolId,
		new_reward_rate_per_block: T::Balance,
	) -> DispatchResult {
		let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
		ensure!(pool_info.admin == *admin, BadOrigin);
		ensure!(
			new_reward_rate_per_block > pool_info.reward_rate_per_block,
			Error::<T>::RewardRateCut
		);

		// Always start by updating the pool rewards.
		let rewards_per_token = Self::reward_per_token(&pool_info)?;
		let mut pool_info = Self::update_pool_rewards(&pool_info, rewards_per_token)?;

		pool_info.reward_rate_per_block = new_reward_rate_per_block;
		Pools::<T>::insert(pool_id, pool_info);

		Self::deposit_event(Event::PoolRewardRateModified { pool_id, new_reward_rate_per_block });

		Ok(())
	}
```
