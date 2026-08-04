### Title
Reward-per-token truncated division permanently forfeits `pallet-asset-rewards` staker rewards - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards`'s `reward_per_token` function computes the accrued reward index as `reward_rate_per_block * elapsed_blocks * PRECISION_SCALING_FACTOR / total_tokens_staked`, where `PRECISION_SCALING_FACTOR` is a fixed constant of only `4096` (12 bits) [1](#0-0) . This is the exact same class of bug as the Locke.sol report: an integer-division scaling factor that is too small relative to `total_tokens_staked` causes the numerator to be rounded down to (or toward) zero, and this loss is **not recoverable** on a later call because `last_update_block`/`reward_per_token_stored` advance monotonically and the "lost" time window is never re-included in a future numerator.

### Finding Description
The reward accrual math is:

```rust
Ok(pool_info.reward_per_token_stored.ensure_add(
    pool_info
        .reward_rate_per_block
        .ensure_mul(rewardable_blocks_elapsed.into())?
        .ensure_mul(PRECISION_SCALING_FACTOR.into())?
        .ensure_div(pool_info.total_tokens_staked)?,
)?)
``` [2](#0-1) 

and the per-staker reward derivation reverses the same scaling factor:

```rust
Ok(staker_info
    .amount
    .ensure_mul(reward_per_token.ensure_sub(staker_info.reward_per_token_paid)?)?
    .ensure_div(PRECISION_SCALING_FACTOR.into())?
    .ensure_add(staker_info.rewards)?)
``` [3](#0-2) 

This is structurally identical to the reported Locke.sol bug: `rewards = timeElapsed * rewardRate * scalingFactor / totalStaked`. The Locke report explicitly lists the conditions that trigger truncation loss: low reward-token decimals, frequent updates (small `elapsed_blocks`), and large `totalVirtualBalance`/stake. All of these conditions apply directly here:

- `reward_rate_per_block` is denominated in the reward asset's raw units. For a reward asset with 6 decimals (as in the report's example) and moderate reward budgets, `reward_rate_per_block` can easily be a value in the single digits or tens.
- `total_tokens_staked` is denominated in the staked asset's raw units, which for an 18-decimal LP/staked asset with meaningful stake (e.g. `100_000 * 10**18`) is enormous.
- `PRECISION_SCALING_FACTOR` is a hard-coded `4096`, i.e. roughly `2^12` ≈ `4.1 * 10^3` — far too small to compensate for the ratio between a tiny `reward_rate_per_block` and an 18-decimal `total_tokens_staked`. Compare this to `pallet-nomination-pools`, which uses a `FixedU128`-based `RewardCounter` scaled by `10^18` and contains an explicit accuracy analysis proving it is safe at Polkadot-scale values [4](#0-3) . `pallet-asset-rewards` has no equivalent accuracy proof and uses a scaling factor four to five orders of magnitude smaller.
- `update_pool_and_staker_rewards`/`reward_per_token` is invoked on every `stake`, `unstake`, and `harvest_rewards` call [5](#0-4) [6](#0-5) , so any staker (or any third party interacting with the pool, since these are unprivileged public extrinsics) can trigger frequent, small `rewardable_blocks_elapsed` windows, maximizing the chance of numerator truncation to zero.

When `reward_rate_per_block * rewardable_blocks_elapsed * 4096 < total_tokens_staked`, the integer division yields `0`, so `reward_per_token_stored` does not increase for that interval even though real reward tokens were streaming during that period (the pool account is still being funded/entitled to distribute per `reward_rate_per_block * elapsed_blocks`, but the per-token index used to allocate it to stakers does not move). Because `last_update_block` is updated unconditionally to `now` in `update_pool_rewards` regardless of whether `reward_per_token_stored` changed [7](#0-6) , that elapsed block window is consumed and can never contribute to `reward_per_token` again — the reward for that interval is permanently forfeited, exactly matching the "user loses reward in this update round" impact from the Locke.sol report.

No existing guard prevents this: `ensure_mul`/`ensure_div` (via `EnsureMul`/`EnsureDiv`) only guard against overflow, not against precision loss/rounding to zero; there is no minimum-elapsed-blocks check, no accumulator/remainder carry-forward, and no dynamic scaling based on asset decimals.

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for reward payouts: reward tokens that are nominally due to stakers based on `reward_rate_per_block` and elapsed time are silently and permanently lost rather than paid out, with no way to reclaim them (they simply remain undistributed dust in the pool account, effectively going to whichever staker happens to trigger the *next* non-truncating update, or to nobody if the pool eventually gets `cleanup_pool`'d, returning the residual to the admin — meaning value is misallocated to the admin instead of the intended stakers) [8](#0-7) . This is a fund-loss/misallocation bug in a shipped FRAME pallet usable by any parachain, not merely a rounding inefficiency — repeated triggering can cause stakers to permanently lose a meaningful fraction of intended rewards.

### Likelihood Explanation
High. Any staker or third party can call `stake`, `unstake`, or `harvest_rewards` at will and for free (aside from normal transaction fees) with no cooldown, so triggering many small `rewardable_blocks_elapsed` windows is trivial and requires no privileged access, malicious relayer/validator, or governance action — it is directly reachable through ordinary public extrinsics. The likelihood is highest for exactly the parameter combinations already known to be realistic (low-decimal reward asset, high-decimal/high-value staked asset, high stake amounts near the low end of `reward_rate_per_block`), which are configuration choices entirely under the pool admin's/market's control, not attacker-controlled exotic edge cases.

### Recommendation
Increase `PRECISION_SCALING_FACTOR` to a much larger fixed-point base (e.g. `10^18`, as `pallet-nomination-pools` uses for its `RewardCounter`), or switch `reward_per_token` to use a proper fixed-point type (`FixedU128`/`Perquintill`) with a documented accuracy proof similar to the one in `pallet-nomination-pools`. Additionally, consider tracking/carrying forward the truncated remainder of `reward_rate_per_block * elapsed_blocks * SCALE % total_tokens_staked` so that fractional accrual is not lost across updates, rather than resetting `last_update_block` unconditionally.

### Proof of Concept
1. `create_pool` with `reward_asset_id` having 6 decimals and `reward_rate_per_block` set to, e.g., `10` (raw units) and `staked_asset_id` with 18 decimals.
2. `stake(pool_id, 100_000 * 10**18)` from account A at block `N`.
3. At block `N + 1` (one block later, e.g. by calling `harvest_rewards` or having account B call `stake(pool_id, 1)`), `reward_per_token` computes:
   `10 * 1 * 4096 / (100_000 * 10**18) = 40960 / 10^23 = 0` (integer division truncates to 0).
4. `reward_per_token_stored` remains unchanged, `last_update_block` advances to `N+1`; the reward that should have accrued for block `N→N+1` is permanently lost and can never be recovered even though the pool account is still holding/streaming the corresponding reward tokens intended for that block.
5. Repeating this every block (trivially done by an unprivileged account) causes the pool to systematically under-pay stakers relative to `reward_rate_per_block * total_elapsed_blocks`, with the shortfall growing unboundedly over the life of the pool. [2](#0-1)

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L690-729)
```rust
		/// Cleanup a pool.
		///
		/// Origin must be the pool admin.
		///
		/// Cleanup storage, release any associated storage cost and return the remaining reward
		/// tokens to the admin.
		#[pallet::call_index(8)]
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L787-810)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L815-824)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1472-1509)
```rust
		// * accuracy notes regarding the multiplication in `checked_from_rational`:
		// `current_payout_balance` is a subset of the total_issuance at the very worse.
		// `bonded_points` are similarly, in a non-slashed pool, have the same granularity as
		// balance, and are thus below within the range of total_issuance. In the worse case
		// scenario, for `saturating_from_rational`, we have:
		//
		// dot_total_issuance * 10^18 / `minJoinBond`
		//
		// assuming `MinJoinBond == ED`
		//
		// dot_total_issuance * 10^18 / 10^10 = dot_total_issuance * 10^8
		//
		// which, with the current numbers, is a miniscule fraction of the u128 capacity.
		//
		// Thus, adding two values of type reward counter should be safe for ages in a chain like
		// Polkadot. The important note here is that `reward_pool.last_recorded_reward_counter` only
		// ever accumulates, but its semantics imply that it is less than total_issuance, when
		// represented as `FixedU128`, which means it is less than `total_issuance * 10^18`.
		//
		// * accuracy notes regarding `checked_from_rational` collapsing to zero, meaning that no
		//   reward can be claimed:
		//
		// largest `bonded_points`, such that the reward counter is non-zero, with `FixedU128` will
		// be when the payout is being computed. This essentially means `payout/bonded_points` needs
		// to be more than 1/1^18. Thus, assuming that `bonded_points` will always be less than `10
		// * dot_total_issuance`, if the reward_counter is the smallest possible value, the value of
		//   the
		// reward being calculated is:
		//
		// x / 10^20 = 1/ 10^18
		//
		// x = 100
		//
		// which is basically 10^-8 DOTs. See `smallest_claimable_reward` for an example of this.
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;
```
