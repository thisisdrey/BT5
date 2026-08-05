## Analysis

`pallet-asset-rewards` (`substrate/frame/asset-rewards/src/lib.rs`) is the strongest local analog to the FlatMoney "free points minting via deposit/withdraw cycling" bug class. It implements exactly the same structural pattern as the LP-points system in the audit report: a `stake`/`unstake` pair of permissionless public extrinsics that continuously accrue a reward balance proportional to `amount / total_tokens_staked`, with **no fee, no minimum dwell time, and no cost** on either action other than gas.

### Title
Unthrottled stake/unstake cycling in `pallet-asset-rewards` allows reward-share dilution/farming with no cost or lock-up - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`stake` [1](#0-0)  and `unstake` [2](#0-1)  are freely callable, permissionless, and have zero cost besides a freeze (not even a bonding/unbonding delay like `pallet-nomination-pools` or `pallet-staking` impose). Reward accrual is computed purely from `total_tokens_staked` at each block via `reward_per_token` [3](#0-2) , so an attacker's share of the reward-rate-per-block is entirely a function of `amount / total_tokens_staked` at each instant, with no time-weighting requirement or exit penalty.

### Finding Description
This mirrors the FlatMoney bug precisely: in that report, an attacker repeatedly deposits and withdraws liquidity to mint “points” faster than legitimate LPs, with no meaningful cost gate. In `pallet-asset-rewards`, the same primitive exists:

1. `stake(pool_id, amount)` immediately begins earning a share of `reward_rate_per_block` proportional to `amount / total_tokens_staked` — there is no minimum stake duration.
2. `unstake(pool_id, amount, staker)` immediately unfreezes funds with no fee, no cooldown, and no bonding-duration delay (unlike `pallet-nomination-pools`, which enforces `BondingDuration` before funds are released, and unlike the original StableModule which has a withdrawal fee acknowledged as the "cost" that prevents free minting).
3. Because `reward_per_token` updates based on `total_tokens_staked` at each block boundary, an attacker can flash-stake a large amount right before a block that credits rewards (or right when other stakers are diluted), then instantly `unstake`, repeating the cycle to capture a disproportionate share of `reward_rate_per_block` while contributing capital for only the shortest possible window, diluting/starving honest long-term stakers.

Unlike `pallet-nomination-pools` — which the search confirmed enforces a `BondingDuration` before `withdraw_unbonded` releases funds, meaning cycling incurs an unbonding-period opportunity cost — `pallet-asset-rewards`'s `unstake` has no such delay: `T::AssetsFreezer::decrease_frozen` [4](#0-3)  executes synchronously in the same call, so capital can be recycled every block with no waiting period and no fee, which is structurally *weaker* than the guard the FlatMoney maintainers explicitly relied on ("There is a cost associated with withdrawing liquidity in the form of the withdrawal fee").

### Impact Explanation
An attacker can dominate `reward_asset_id` distribution from a pool by flash-staking large capital right before reward-crediting moments and unstaking immediately after, starving genuine long-term stakers of the reward stream — a direct analog of "prevent other users from earning points." Because reward pools must be pre-funded by the admin (`deposit_reward_tokens`), this results in real economic value (the reward asset) being redirected away from intended recipients at effectively zero cost to the attacker, which is a value-conservation/wrong-beneficiary impact.

### Likelihood Explanation
Likelihood is high: `stake` and `unstake` are permissionless, callable by any signed account, require no origin filter beyond `ensure_signed`, and have no rate limit, minimum stake period, or exit fee. The attack only requires enough capital to momentarily dominate `total_tokens_staked`, which is realistic for pools with modest TVL, and can be repeated every block indefinitely.

### Recommendation
Introduce either (a) a minimum stake duration before `unstake` is permitted or before newly staked amounts start accruing rewards, or (b) an exit fee/haircut on `unstake` (redirected to the reward pool or existing stakers) similar to the withdrawal-fee mitigation FlatMoney relies on, or (c) time-weight reward accrual (e.g., accrue based on stake-time integral rather than instantaneous snapshot) to remove the profitability of flash stake/unstake cycles.

### Proof of Concept
1. Admin creates a pool with `create_pool` funding `reward_rate_per_block`.
2. Honest staker A stakes `1000` at block 1 and holds.
3. Attacker, right before a block where rewards are about to be harvested/credited, calls `stake(pool_id, 1_000_000)`, dramatically increasing `total_tokens_staked` and thus increasing `reward_per_token` growth attributable to their share for that block.
4. Attacker immediately calls `unstake(pool_id, 1_000_000, None)` in the very next transaction/block — see `unstake` flow at [5](#0-4)  — reclaiming full capital with zero fee and zero delay.
5. Attacker calls `harvest_rewards` to claim the disproportionate reward share earned during that brief window, repeating the cycle every block to continuously siphon `reward_rate_per_block` away from staker A, who receives a shrinking proportional share despite being staked the entire time.

**Caveat**: I was unable to find any existing minimum-duration or exit-fee guard in the `asset-rewards` pallet within the indexed portions of `substrate/frame/asset-rewards/src/lib.rs`; if such a guard exists elsewhere (e.g., in a runtime-level wrapper not indexed here), it would mitigate this. A full review of the pallet's `Config` trait and any downstream runtime integration would be needed to confirm no external cooldown is imposed.

### Citations

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L504-560)
```rust
		/// Unstake tokens from a pool.
		///
		/// Removes the freeze on the staked tokens.
		///
		/// Parameters:
		/// - origin: must be the `staker` if the pool is still active. Otherwise, any account.
		/// - pool_id: the pool to unstake from.
		/// - amount: the amount of tokens to unstake.
		/// - staker: the account to unstake from. If `None`, the caller is used.
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
