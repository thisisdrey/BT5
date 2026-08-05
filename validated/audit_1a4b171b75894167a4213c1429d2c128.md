Confirmed: the current repository code for `harvest_rewards` at `substrate/frame/asset-rewards/src/lib.rs` lines 568-615 indeed omits any `Pools::<T>::insert(pool_id, pool_info)` call. It only destructures `pool_info` from `update_pool_and_staker_rewards`, uses it to read `reward_asset_id` and `account` for the transfer, and updates `PoolStakers` — but never writes the recomputed `pool_info` (with advanced `last_update_block` and `reward_per_token_stored`) back to the `Pools` storage map, unlike `stake` (lines 473-502) and `unstake` (lines 513-545), which both explicitly call `Pools::<T>::insert(pool_id, pool_info)` after calling the same helper.This confirms the mechanics: `update_pool_rewards` explicitly documents "NOTE: this function has no side-effects. Side-effects such as storage modifications are the responsibility of the caller." [1](#0-0) , and `reward_per_token` derives the accumulator purely from `pool_info.last_update_block` and `pool_info.reward_per_token_stored` read from the passed-in (stale, unwritten) `pool_info` [2](#0-1) . Since `harvest_rewards` never calls the "caller's responsibility" storage write, every subsequent call to `stake`, `unstake`, or `harvest_rewards` on the same pool will recompute `reward_per_token` using the same stale `last_update_block`, double counting the elapsed-block interval that was already paid out. The `PoolStakers` update in `harvest_rewards` correctly advances that staker's own `reward_per_token_paid` [3](#0-2) , but the pool-wide accumulator in `Pools` storage remains stale, confirming the over-crediting mechanism described in the claim.

The claim is accurate and matches the actual code in this repository.

Audit Report

## Title
`harvest_rewards` never persists the recomputed pool checkpoint, letting anyone repeatedly double-count elapsed blocks and over-mint reward-per-token for a pool - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`Pallet::update_pool_and_staker_rewards` derives a fresh `reward_per_token_stored` and advances `last_update_block` to the current block, but this update function is documented as having "no side-effects" — storage persistence is the caller's responsibility. `stake` and `unstake` correctly persist the updated `pool_info` via `Pools::<T>::insert(pool_id, pool_info)`, but `harvest_rewards` computes the same updated `pool_info` and only uses it to authorize the token transfer, never writing it back to the `Pools` storage map.

## Finding Description
`reward_per_token()` computes the pool's accumulated reward-per-token by multiplying `reward_rate_per_block` by the number of blocks elapsed since `pool_info.last_update_block`, added to the stored `reward_per_token_stored` [4](#0-3) . `harvest_rewards` calls `update_pool_and_staker_rewards`, which internally calls `update_pool_rewards` — explicitly documented as having no side-effects, with storage modification left to the caller [5](#0-4) . Unlike `stake` (which calls `Pools::<T>::insert(pool_id, pool_info)` at line 491) and `unstake` (same pattern at line 543), `harvest_rewards` never persists the recomputed `pool_info` back into `Pools::<T>` [6](#0-5) . As a result, `last_update_block` and `reward_per_token_stored` remain stale in storage after every `harvest_rewards` call, causing the next reader of the pool (`stake`, `unstake`, or another `harvest_rewards`) to recompute `reward_per_token` over an elapsed-block window that overlaps with, and double-counts, an interval already paid out.

## Impact Explanation
This allows an unprivileged staker to trigger over-distribution of the pool's `reward_asset_id` tokens beyond the amount intended by `reward_rate_per_block`, since rewards are paid via `T::Assets::transfer` from the pool's own account [7](#0-6) . Repeated exploitation can drain the pool's reward-asset balance faster than intended, causing fund loss for later legitimate claimants — matching the "duplicate settlement or payout" impact class.

## Likelihood Explanation
High. `harvest_rewards` is a normal, public, frequently used extrinsic requiring no privileged role — any staker with a staked position and non-zero pending reward can trigger it, and interleaving with other stakers' `stake`/`unstake`/`harvest_rewards` calls reliably reproduces the double-counted window.

## Recommendation
`harvest_rewards` must persist the pool-level checkpoint identically to `stake`/`unstake`: after computing `(pool_info, staker_info) = Self::update_pool_and_staker_rewards(...)`, call `Pools::<T>::insert(pool_id, pool_info)` so `last_update_block` and `reward_per_token_stored` are advanced in storage on every code path that reads them.

## Proof of Concept
1. Admin creates a pool with `reward_rate_per_block = R`, funds it with reward tokens.
2. Staker A stakes `X` tokens at block `M` (via `stake`, which persists `pool_info`).
3. At block `N`, Staker A calls `harvest_rewards`; `reward_per_token` is computed using elapsed `N - M` and paid out, but `Pools::<T>::get(pool_id)` still shows `last_update_block = M` afterward since no `insert` occurs.
4. Staker B stakes at block `N2 > N` via `stake`; its call to `update_pool_and_staker_rewards` recomputes `reward_per_token` using elapsed `N2 - M` (not `N2 - N`) against the stale stored value, double-counting the `[M, N]` interval already paid to Staker A.
5. Cumulative rewards claimable across all stakers exceed `R * (N2 - M)`, draining the pool's reward-asset balance faster than intended.

### Citations

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L767-784)
```rust
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
