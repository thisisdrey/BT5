### Title
Reward accrual in `pallet-asset-rewards` is unbacked by actual pool funds, with no on-chain solvency invariant, causing race-based fund loss/lock for stakers - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` computes each staker's claimable reward purely as a function of time and stake share (`reward_per_token`), completely decoupled from whether the pool's reward-asset account actually holds enough balance to honor that promise. Unlike `pallet-nomination-pools`, which has an explicit `try_state` solvency check (`pending_rewards_lt_leftover_bal`) and a remediation extrinsic (`adjust_pool_deposit`), `pallet-asset-rewards` has neither. This is the direct local analog of the reported bug class: virtual fee/reward accounting is updated ("distributed") without any guarantee that the accounting is backed by real transferable balance, and no invariant enforces `sum(promised rewards) <= pool_account_balance`.

### Finding Description
The reward accrual math lives in `reward_per_token`, `update_pool_rewards`, and `derive_rewards`: [1](#0-0) 

`reward_per_token` grows `reward_per_token_stored` strictly as `reward_rate_per_block * elapsed_blocks / total_tokens_staked`, with no reference at all to `T::Assets::balance(pool_info.reward_asset_id, &pool_info.account)`. This value feeds directly into each staker's `rewards` balance via `derive_rewards`/`update_pool_and_staker_rewards`.

The only place real tokens move is `harvest_rewards`, which blindly transfers `staker_info.rewards` out of `pool_info.account`: [2](#0-1) 

Nothing before this transfer checks that the pool account's `reducible_balance` for the reward asset is sufficient to cover this staker's accrued amount *plus* all other stakers' already-accrued-but-unclaimed amounts. The pallet's own doc comment concedes this is a bookkeeping-only convention, not an enforced invariant: "Care should be taken by the pool operator to keep pool accounts adequately funded with the reward asset." [3](#0-2) 

Reward funding itself is a simple, unchecked transfer with no linkage back to the promised `reward_rate_per_block * (expiry_block - creation_block)` total: [4](#0-3) 

And the admin-controlled reward-rate increase (`set_pool_reward_rate_per_block`) can raise the promised accrual rate at any time without any check that the pool account holds (or will hold) enough reward asset to cover the new, larger promise for the remaining pool lifetime: [5](#0-4) 

By contrast, `pallet-nomination-pools`'s `RewardPool` ties `last_recorded_total_payouts`/`current_reward_counter` to `current_balance` (an actual `reducible_balance` query) and additionally runs a dedicated solvency check in `do_try_state`: [6](#0-5) [7](#0-6) 

`pallet-asset-rewards` contains no equivalent `current_balance`-gated accrual, no `try_state` solvency check, and no remediation call analogous to `adjust_pool_deposit`. A grep for `try_state`/`reducible_balance`/`current_balance` in the pallet returns nothing beyond the accrual-unrelated Config trait usage, confirming the check simply does not exist.

### Impact Explanation
Any unprivileged staker can be caught in a state where their `PoolStakers` entry shows a nonzero `rewards` balance that the protocol has "promised" via `reward_per_token_stored`, but the pool account (`pool_info.account`) does not actually hold enough of the reward asset to pay it — this can happen simply through normal pool lifecycle (rate increases, expiry extensions, or an operator's underestimation of required top-ups), with no on-chain code path preventing it. When this occurs:
- `harvest_rewards` for the unlucky staker(s) called later will revert (fund lock: reward is stuck, uncollectible until further top-up, if any).
- Alternatively, if `Preservation::Expendable` transfers partially drain the account down toward zero, the first stakers to call `harvest_rewards` race to drain the shared pot, so payouts are effectively **first-come-first-served** rather than proportional to accrued entitlement — a wrong-beneficiary/wrong-amount settlement outcome, not the intended "each staker gets their proportional share" invariant.
- Because reward-rate increases are gated only by `CreatePoolOrigin`/admin without any balance-sufficiency check, the promised total obligation can be pushed above the actual funded balance at any time, silently creating pool insolvency that is invisible to stakers until they try to harvest.

This directly matches the report's core complaint: "the mechanism by which LP Token gains fees... simply updates the total underlying token amount... without actually transferring token" and "there may need to be some sort of invariant... no such check takes place on-chain."

### Likelihood Explanation
High likelihood of occurring in practice without any malicious actor: pool admins are expected (per the doc comment itself) to manually keep the account funded, and the rate can only be *increased*, never decreased (`RewardRateCut` error), and expiry can only be extended (`ExpiryCut` error) — both irreversible one-way admin actions that increase the pallet's total future obligation. Since nothing in code cross-checks obligation vs. funded balance at rate-increase, expiry-extension, or harvest time, under-funding is a normal, easily reached state, not an edge case requiring privileged malice. The trigger condition (harvest attempted when accrued global obligation exceeds actual balance) requires no special access — any staker calling the public `harvest_rewards` extrinsic can hit it.

### Recommendation
1. Add a solvency check before honoring `harvest_rewards`/before allowing further un-collateralized accrual: compare `T::Assets::reducible_balance(reward_asset_id, &pool_info.account, ...)` against the sum of all outstanding/unclaimed staker rewards for the pool (or at minimum against the specific claim being paid, saturating/clamping the payout if insufficient rather than reverting non-deterministically).
2. Enforce an invariant at `set_pool_reward_rate_per_block` / `set_pool_expiry_block` time: the new promised total obligation for the remaining pool lifetime must not exceed the pool account's current reward-asset balance (or require a matching `deposit_reward_tokens` in the same transaction).
3. Add a `do_try_state`/runtime invariant check analogous to nomination-pools' `pending_rewards_lt_leftover_bal`, comparing total unclaimed promised rewards against the pool account's actual balance, to make under-funding detectable rather than silently discovered by users at harvest time.
4. Document explicitly (and enforce in code, not just in comments) the relationship between "promised reward accrual" and "actual funded balance," removing the current reliance on operator diligence alone.

### Proof of Concept
Conceptual reproduction path (no privileged/malicious actor required beyond a normal admin performing allowed, non-malicious operations):
1. Admin creates a pool via `create_pool` with `reward_rate_per_block = R1` and funds the pool account with exactly enough reward asset to cover `R1` until `expiry_block`.
2. Multiple stakers `stake` into the pool; `reward_per_token_stored` accrues at rate `R1`, matching the funded balance so far — solvent.
3. Admin calls `set_pool_reward_rate_per_block` to raise the rate to `R2 > R1` (allowed unconditionally by the pallet, no balance check performed — see lines 617-635) without depositing additional reward tokens.
4. From this point, `reward_per_token_stored` accrues faster than the pool account's real balance can support. No code path detects or halts this.
5. Once accrued promised rewards across all stakers exceed the pool account balance, the first staker(s) to call `harvest_rewards` (lines 562-615) succeed and drain the account; subsequent stakers' `harvest_rewards` calls fail with an `Assets::transfer` error (their accrued `rewards` value remains stuck, unpayable) — an unprivileged, non-malicious, purely mechanical fund-lock/mis-settlement outcome traceable to the missing invariant identified above.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L35-38)
```rust
//! Reward assets pending distribution are held in an account unique to each pool.
//!
//! Care should be taken by the pool operator to keep pool accounts adequately funded with the
//! reward asset.
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L562-615)
```rust
		/// Harvest unclaimed pool rewards.
		///
		/// Parameters:
		/// - origin: must be the `staker` if the pool is still active. Otherwise, any account.
		/// - pool_id: the pool to harvest from.
		/// - staker: the account for which to harvest rewards. If `None`, the caller is used.
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L617-635)
```rust
		/// Modify a pool reward rate.
		///
		/// Currently the reward rate can only be increased.
		///
		/// Only the pool admin may perform this operation.
		#[pallet::call_index(4)]
		pub fn set_pool_reward_rate_per_block(
			origin: OriginFor<T>,
			pool_id: PoolId,
			new_reward_rate_per_block: T::Balance,
		) -> DispatchResult {
			let caller = T::CreatePoolOrigin::ensure_origin(origin.clone())
				.or_else(|_| ensure_signed(origin))?;
			<Self as RewardsPool<_>>::set_pool_reward_rate_per_block(
				&caller,
				pool_id,
				new_reward_rate_per_block,
			)
		}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L667-688)
```rust
		/// Convenience method to deposit reward tokens into a pool.
		///
		/// This method is not strictly necessary (tokens could be transferred directly to the
		/// pool pot address), but is provided for convenience so manual derivation of the
		/// account id is not required.
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L786-824)
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1514-1523)
```rust
	/// Current free balance of the reward pool.
	///
	/// This is sum of all the rewards that are claimable by pool members.
	fn current_balance(id: PoolId) -> BalanceOf<T> {
		T::Currency::reducible_balance(
			&Pallet::<T>::generate_reward_account(id),
			Preservation::Expendable,
			Fortitude::Polite,
		)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3998-4016)
```rust
		RewardPools::<T>::iter_keys().try_for_each(|id| -> Result<(), TryRuntimeError> {
			// the sum of the pending rewards must be less than the leftover balance. Since the
			// reward math rounds down, we might accumulate some dust here.
			let pending_rewards_lt_leftover_bal = RewardPool::<T>::current_balance(id) >=
				pools_members_pending_rewards.get(&id).copied().unwrap_or_default();

			// If this happens, this is most likely due to an old bug and not a recent code change.
			// We warn about this in try-runtime checks but do not panic.
			if !pending_rewards_lt_leftover_bal {
				log!(
					warn,
					"pool {:?}, sum pending rewards = {:?}, remaining balance = {:?}",
					id,
					pools_members_pending_rewards.get(&id),
					RewardPool::<T>::current_balance(id)
				);
			}
			Ok(())
		})?;
```
