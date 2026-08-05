Confirmed. `harvest_rewards` at [1](#0-0)  calls `update_pool_and_staker_rewards` and never persists the resulting `pool_info` back into the `Pools` storage map — unlike `stake` and `unstake`, which explicitly call `Pools::<T>::insert(pool_id, pool_info)` after the same update helper. [2](#0-1)  and [3](#0-2)  both persist the updated pool state, while [4](#0-3)  computes it but the function proceeds directly to the transfer and event without any `Pools::<T>::insert` call anywhere in the function body through its `Ok(())` return at line 614. The `reward_per_token` calculation depends on `pool_info.last_update_block`/`reward_per_token_stored` being advanced in storage, so the missing persistence lets the same reward interval be recomputed and credited to other stakers on their next `stake`/`unstake`/`harvest_rewards` call, causing double-accrual against the pool's reward-asset balance.

Audit Report

## Title
Missing pool-state persistence in `harvest_rewards` causes reward double-accrual across stakers - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`Pallet::harvest_rewards` recomputes the pool's `reward_per_token_stored` and `last_update_block` locally via `update_pool_and_staker_rewards`, but unlike every other mutating extrinsic in the pallet (`stake`, `unstake`, `set_pool_reward_rate_per_block`), it never writes the updated `PoolInfo` back into the `Pools` storage map.

## Finding Description
`stake` and `unstake` both call `update_pool_and_staker_rewards` and then persist the result with `Pools::<T>::insert(pool_id, pool_info)`. In contrast, `harvest_rewards` calls the same helper, uses `pool_info.reward_asset_id`/`pool_info.account` for the transfer, but only ever writes `PoolStakers::<T>::insert`; it never calls `Pools::<T>::insert` for the freshly computed `pool_info`.

`reward_per_token` is derived from `pool_info.last_update_block` and `pool_info.reward_per_token_stored`, both of which `update_pool_rewards` advances to "now" every time it is invoked. Because `harvest_rewards` throws away this advance, the on-chain `Pools` entry keeps its stale `last_update_block`/`reward_per_token_stored`. The next staker who triggers `update_pool_and_staker_rewards` (via `stake`, `unstake`, or another `harvest_rewards`) will recompute `reward_per_token` over the same block range that was already paid out to the previous harvester, inflating `reward_per_token_stored` a second time for that interval.

The staker's individual `reward_per_token_paid` is advanced correctly in `PoolStakers` (so that specific staker cannot re-claim the same rewards), but the pool-wide `reward_per_token_stored`/`last_update_block` clock is not, so other stakers active during that same interval will have their rewards computed as if the interval had not yet been paid out — duplicating reward-asset entitlement beyond what the pool's `reward_rate_per_block * elapsed_blocks` and deposited balance justify. Any unprivileged staker triggers this simply by calling `harvest_rewards` on their own stake.

## Impact Explanation
This breaks the pool's reward-conservation invariant: total rewards claimable by stakers should equal `reward_rate_per_block * elapsed_blocks` exactly once per block interval, funded by whatever was deposited via `deposit_reward_tokens`. The missing persistence means that interval is re-counted, letting stakers collectively claim more reward-asset value than was funded into the pool account, eventually causing `T::Assets::transfer` in `harvest_rewards` to fail with insufficient pool balance once the unfunded surplus is exhausted — i.e., legitimate stakers get stuck unable to harvest already-accrued real rewards, matching the "duplicate settlement" impact class.

## Likelihood Explanation
High: `harvest_rewards` is a fully public, unprivileged extrinsic (`ensure_signed` only) callable by any staker at any time, and the missing `Pools::insert` is present on every code path of the function, not an edge case.

## Recommendation
Add `Pools::<T>::insert(pool_id, pool_info);` in `harvest_rewards` immediately after computing the updated `pool_info`, mirroring the pattern used in `stake`, `unstake`, and `set_pool_reward_rate_per_block`, so the pool's `last_update_block`/`reward_per_token_stored` are always advanced atomically alongside per-staker settlement.

## Proof of Concept
1. `create_pool` with `reward_rate_per_block = R`, deposit reward tokens via `deposit_reward_tokens`.
2. Staker A calls `stake` (pool state persisted, `last_update_block = T0`).
3. Advance N blocks. Staker A calls `harvest_rewards`: locally computes `reward_per_token_stored` advanced to block `T0+N` and pays out `R*N` (scaled) to A; `PoolStakers` for A is updated, but `Pools` storage still shows `last_update_block = T0`.
4. Staker B (staked before block T0) calls `harvest_rewards` or `unstake`: `update_pool_and_staker_rewards` recomputes `reward_per_token` for the same interval `T0`→`T0+N` again, crediting B with reward for blocks already paid out to A.
5. Total rewards paid out for interval `T0..T0+N` now exceed `R*N`, repeatable until `T::Assets::transfer` in a later `harvest_rewards` fails with insufficient pool balance, permanently blocking legitimate stakers' harvest of correctly-earned rewards.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L489-492)
```rust
			// Update Pools.
			pool_info.total_tokens_staked.ensure_add_assign(amount)?;

			Pools::<T>::insert(pool_id, pool_info);
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L543-545)
```rust
			// Update Pools.
			pool_info.total_tokens_staked.ensure_sub_assign(amount)?;
			Pools::<T>::insert(pool_id, pool_info);
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L577-615)
```rust
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
