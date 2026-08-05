## Analysis

The Solidity bug's core invariant break is: **a stored "last update" timestamp/block can be advanced past the period's end-cap, so the next `min(now, periodEnd) - lastUpdate` calculation underflows and permanently reverts for all callers after the first**.

This exact defect exists in `pallet-asset-rewards`.

### Title
Permanent DoS / fund lock in `pallet-asset-rewards` reward accounting due to `last_update_block` advancing past `expiry_block`, causing `reward_per_token()` to underflow-revert for all subsequent stakers — (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`Pallet::reward_per_token` computes elapsed blocks as `last_block_reward_applicable(expiry_block).ensure_sub(last_update_block)`, where `last_block_reward_applicable` caps at `expiry_block`, but `update_pool_rewards` always sets `last_update_block` to the *current* block number, uncapped. Once any account interacts with the pool at a block past `expiry_block` (any of `stake`, `unstake`, `harvest_rewards`), `last_update_block` becomes greater than `expiry_block`. Every later call to any of these extrinsics on the same pool then underflows in `ensure_sub`, returns an `ArithmeticError`, and the whole extrinsic reverts — locking out every other staker of that pool from harvesting rewards or unstaking.

### Finding Description
`last_block_reward_applicable` returns `min(now, expiry_block)`: [1](#0-0) 

`reward_per_token` subtracts `last_update_block` from that capped value using `ensure_sub`, which returns an error (not saturating) on underflow: [2](#0-1) 

`update_pool_rewards` sets `last_update_block` to the *uncapped* current block number every time it's called, including calls made after `expiry_block` has passed: [3](#0-2) 

Sequence:
1. Pool expires at `expiry_block = E`. Multiple stakers are still owed unclaimed rewards.
2. At block `N > E`, staker A calls `harvest_rewards` (or `unstake`). This is permitted post-expiry for any staker (`ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin)`), and succeeds: `reward_per_token` correctly computes elapsed = `E - last_update_block_old` (still valid because `last_update_block_old <= E` at this point), then `update_pool_rewards` writes `last_update_block = N` (> `E`) into storage. [4](#0-3) [5](#0-4) 
3. Any subsequent staker B calling `harvest_rewards`, `unstake`, or an existing staker calling `stake` on the same pool, at block `N' >= N`, triggers `reward_per_token` again: `last_block_reward_applicable(E) = E` (since `N' > E`), and `E.ensure_sub(last_update_block = N)` underflows because `N > E`. `ensure_sub` returns `Err(ArithmeticError::Underflow)`, which propagates and reverts the entire extrinsic.
4. Because the corrupted `last_update_block` value in `Pools` storage is never corrected downward (it only moves forward on success), **every future call to `stake`, `unstake`, or `harvest_rewards` on this pool by any account other than the one lucky first caller will fail forever**, unless a privileged admin extends `expiry_block` again via `set_pool_expiry_block` (an admin-privileged path, not something an ordinary staker can trigger).

The corrupted value is `Pools::<T>::get(pool_id).last_update_block`, which after step 2 exceeds `expiry_block`, permanently breaking the invariant `last_update_block <= expiry_block` (post-expiry) that `reward_per_token` implicitly relies on. No existing guard enforces this invariant: `update_pool_rewards` never clamps `last_update_block` to `expiry_block`, and `ensure_sub` is unforgiving (unlike `saturating_sub`), so the underflow is surfaced as a hard revert rather than silently producing zero.

### Impact Explanation
This is a permanent user-fund lock: any staker other than the first post-expiry claimant becomes unable to `harvest_rewards` (retrieve already-earned reward tokens) or `unstake` (retrieve frozen staked tokens) from that pool. Since `unstake` also calls `update_pool_and_staker_rewards` → `reward_per_token`, even unstaking of the *staked* principal (not just rewards) is blocked, trapping frozen funds indefinitely for ordinary, unprivileged users. This matches the "permanent user-fund lock" impact category exactly.

### Likelihood Explanation
High. This will occur in the ordinary lifecycle of any pool with more than one staker whenever the pool is allowed to expire naturally (i.e., no admin action to extend expiry) and rewards/unstakes are claimed after `expiry_block`. The first post-expiry interaction with the pool, by any account, immediately poisons the pool state for everyone else — no attacker sophistication or special conditions are required, only ordinary usage after natural expiry.

### Recommendation
Cap `last_update_block` to `expiry_block` in `update_pool_rewards`, mirroring the recommended Solidity fix pattern:
```rust
new_pool_info.last_update_block =
    Self::last_block_reward_applicable(pool_info.expiry_block);
```
This ensures `last_update_block` never exceeds `expiry_block`, so subsequent calls to `reward_per_token` never underflow. Additionally, consider using `saturating_sub`/checked logic with explicit zero-elapsed handling as defense in depth, consistent with how `last_block_reward_applicable` is intended to gate accrual after expiry.

### Proof of Concept
```rust
// Pool created with expiry_block = E, two stakers `alice` and `bob` both staked before E.
System::set_block_number(E + 1); // past expiry

// Alice successfully harvests/unstakes — this sets Pools[pool_id].last_update_block = E + 1 (> E)
assert_ok!(StakingRewards::harvest_rewards(RuntimeOrigin::signed(alice), pool_id, None));

// Bob's harvest now underflows in reward_per_token: 
// last_block_reward_applicable(E) = E,  E.ensure_sub(E + 1) -> Err(ArithmeticError::Underflow)
assert_err!(
    StakingRewards::harvest_rewards(RuntimeOrigin::signed(bob), pool_id, None),
    // fails with an arithmetic/underflow-derived DispatchError
);

// Bob's unstake also fails for the same reason, locking his staked principal:
assert_err!(
    StakingRewards::unstake(RuntimeOrigin::signed(bob), pool_id, staked_amount, None),
    // fails with an arithmetic/underflow-derived DispatchError
);
```

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L524-531)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info = PoolStakers::<T>::get(pool_id, &staker).unwrap_or_default();
			let (mut pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;

```

**File:** substrate/frame/asset-rewards/src/lib.rs (L578-585)
```rust
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);

			let staker_info =
				PoolStakers::<T>::get(pool_id, &staker).ok_or(Error::<T>::NonExistentStaker)?;
			let (pool_info, mut staker_info) =
				Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
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
