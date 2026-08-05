Audit Report

## Title
`update_pool_rewards` fails to cap `last_update_block` at `expiry_block`, permanently bricking expired reward pools and locking staked funds - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`update_pool_rewards` unconditionally sets `last_update_block` to the current block number rather than capping it at `pool_info.expiry_block`, as confirmed at [1](#0-0) . Because `unstake` and `harvest_rewards` explicitly permit calls by any account once the pool has expired (`now > pool_info.expiry_block || caller == staker`), a first post-expiry call succeeds and stores `last_update_block > expiry_block`, and any subsequent call to `stake`/`unstake`/`harvest_rewards` on that pool then underflows in `reward_per_token` and reverts permanently.

## Finding Description
`reward_per_token` computes elapsed rewardable blocks as `last_block_reward_applicable(pool_info.expiry_block).ensure_sub(pool_info.last_update_block)`, which is only safe when `last_update_block <= expiry_block`, as seen at [2](#0-1) . `last_block_reward_applicable` caps its return value at `expiry_block` once `now >= expiry_block`, as shown at [3](#0-2) . However, `update_pool_rewards` sets `new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number()` with no cap, at [4](#0-3) .

`unstake` and `harvest_rewards` both allow any caller to invoke these extrinsics once the pool has expired via the guard `ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin)`, seen at [5](#0-4)  and [6](#0-5) . `stake` performs no expiry check at all before calling `update_pool_and_staker_rewards`, at [7](#0-6) .

Exploit flow:
1. A pool has non-zero `total_tokens_staked` and passes `expiry_block`.
2. The first post-expiry call (e.g. `harvest_rewards` or `unstake` by any account) succeeds because `pool_info.last_update_block` is still `<= expiry_block` at that point; `update_pool_rewards` then sets `last_update_block = now`, a value strictly greater than `expiry_block`.
3. Any later call on the same pool invokes `reward_per_token`, which computes `last_block_reward_applicable(expiry_block) = expiry_block` (since `now > expiry_block`) and then `expiry_block.ensure_sub(last_update_block)`, which underflows because `last_update_block > expiry_block`, returning `Err`.
4. This error propagates through `update_pool_and_staker_rewards` via `?` in `stake`, `unstake`, and `harvest_rewards`, permanently failing every subsequent call on that pool.

Since `unstake` is the only extrinsic that removes the freeze placed by `T::AssetsFreezer::increase_frozen` (seen at [8](#0-7)  and released at [9](#0-8) ), and it can never succeed again once the state is corrupted, all remaining staked tokens in that pool become permanently frozen with no path to recovery through the pallet's public interface.

## Impact Explanation
This matches the "permanent user-fund lock" impact category in the Polkadot SDK impact gate. The bug is triggered purely through ordinary, unprivileged public extrinsics (`stake`, `unstake`, `harvest_rewards`), with no reliance on malicious validators, compromised relayers, leaked keys, or governance abuse. Once triggered, stakers' frozen assets in the affected pool can never be withdrawn again via the pallet, and outstanding rewards can never be harvested.

## Likelihood Explanation
The trigger condition — a pool reaching its `expiry_block` while it still has staked tokens, followed by two ordinary calls after expiry — is a routine part of the pool lifecycle rather than an edge case, and requires no adversarial capability beyond calling public extrinsics that are explicitly designed to be callable by any account once the pool expires. This makes the bug highly likely to manifest in production reward pools without any attacker-specific setup.

## Recommendation
Cap `last_update_block` at `expiry_block` in `update_pool_rewards`, e.g. by setting `new_pool_info.last_update_block = Self::last_block_reward_applicable(pool_info.expiry_block)` instead of the raw current block number, so the invariant `last_update_block <= expiry_block` is preserved after expiry, allowing `unstake`/`harvest_rewards` cleanup calls to keep succeeding indefinitely after a pool expires.

## Proof of Concept
1. Create a pool with `expiry_block = E` and have a staker call `stake` before `E`.
2. Advance the chain to `E + 10` and call `harvest_rewards` (or `unstake` for a partial amount) — this succeeds but sets `pool_info.last_update_block = E + 10`.
3. Advance the chain further to `E + 20` and call `unstake` or `harvest_rewards` again — `reward_per_token` computes `expiry_block(E).ensure_sub(last_update_block(E+10))`, which underflows and returns `Err`, permanently failing the call and leaving the staker's remaining frozen tokens inaccessible through the pallet's extrinsics.

### Citations

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L524-526)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L536-541)
```rust
			T::AssetsFreezer::decrease_frozen(
				pool_info.staked_asset_id.clone(),
				&FreezeReason::Staked.into(),
				&staker,
				amount,
			)?;
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L578-580)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L794-801)
```rust
			let rewardable_blocks_elapsed: u32 =
				match Self::last_block_reward_applicable(pool_info.expiry_block)
					.ensure_sub(pool_info.last_update_block)?
					.try_into()
				{
					Ok(b) => b,
					Err(_) => return Err(Error::<T>::BlockNumberConversionError.into()),
				};
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
