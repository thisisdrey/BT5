The code confirms the vulnerability exactly as claimed. All three extrinsics (`stake`, `unstake`, `harvest_rewards`) call `update_pool_and_staker_rewards`, which calls `reward_per_token` then `update_pool_rewards`.Audit Report

## Title
Permanent DoS / fund lock in `pallet-asset-rewards` reward accounting due to `last_update_block` advancing past `expiry_block`, causing `reward_per_token()` to underflow-revert for all subsequent stakers — (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`Pallet::reward_per_token` computes elapsed blocks as `last_block_reward_applicable(expiry_block).ensure_sub(last_update_block)`, where `last_block_reward_applicable` caps at `expiry_block`, but `update_pool_rewards` always sets `last_update_block` to the uncapped current block number. [1](#0-0) [2](#0-1)  Once any account interacts with the pool at a block past `expiry_block`, `last_update_block` becomes greater than `expiry_block`, and every later call to `stake`, `unstake`, or `harvest_rewards` on the same pool underflows in `ensure_sub`, permanently locking out other stakers' rewards and frozen principal.

## Finding Description
`last_block_reward_applicable` returns `min(now, expiry_block)`: [3](#0-2) . `update_pool_rewards`, called from `update_pool_and_staker_rewards` (itself invoked at the start of `stake`, `unstake`, and `harvest_rewards`), unconditionally sets `last_update_block` to the *current* block number regardless of `expiry_block`: [4](#0-3) [2](#0-1) .

`unstake` and `harvest_rewards` are both permissioned to allow *any* account to call them on behalf of a staker once the pool has expired (`ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin)`), so no privileged action is required to trigger the first post-expiry state write: [5](#0-4) [6](#0-5) .

Exploit flow:
1. Pool expires at block `E`; multiple stakers hold staked/unclaimed balances.
2. At block `N > E`, any staker (or any caller acting for a staker, since post-expiry calls are open) calls `harvest_rewards` or `unstake`. `reward_per_token` computes elapsed = `E - last_update_block_old` (valid, since `last_update_block_old <= E`), succeeds, and `update_pool_rewards` writes `last_update_block = N` into `Pools` storage, unclamped past `E`.
3. Any subsequent call to `stake`, `unstake`, or `harvest_rewards` on this pool (by any other account) at block `N' >= N` computes `last_block_reward_applicable(E) = E` and then `E.ensure_sub(N)`, which underflows since `N > E`. `ensure_sub` returns `Err(ArithmeticError::Underflow)` rather than saturating, reverting the entire extrinsic: [1](#0-0) .
4. Because the corrupted `Pools::<T>::get(pool_id).last_update_block` value only moves forward on success, all further calls to `stake`, `unstake`, and `harvest_rewards` for this pool by any account permanently fail, absent a privileged admin extending `expiry_block`.

No existing guard clamps `last_update_block` to `expiry_block`; `ensure_sub` is unforgiving unlike `saturating_sub`, so this invariant break is unrecoverable through ordinary user action.

## Impact Explanation
This is a permanent user-fund lock reachable via unprivileged public extrinsics: `unstake` also routes through `update_pool_and_staker_rewards` → `reward_per_token`, so once the pool state is corrupted, stakers' frozen principal (not just pending rewards) becomes permanently unrecoverable without privileged admin intervention (`set_pool_expiry_block`), which itself is not something an ordinary staker can invoke. This matches the "permanent user-fund lock" impact category in the Polkadot SDK Impact Gate.

## Likelihood Explanation
High. This occurs in the ordinary lifecycle of any multi-staker pool once the pool naturally expires and any account performs a permitted post-expiry `harvest_rewards`/`unstake` call — no attacker sophistication, privileged role, or unusual configuration is required, only normal usage after natural expiry.

## Recommendation
Clamp `last_update_block` to `expiry_block` in `update_pool_rewards`:
```rust
new_pool_info.last_update_block = Self::last_block_reward_applicable(pool_info.expiry_block);
```
This guarantees `last_update_block` never exceeds `expiry_block`, preventing the underflow in `reward_per_token` for all subsequent calls.

## Proof of Concept
```rust
// Pool created with expiry_block = E; alice and bob both staked before E.
System::set_block_number(E + 1); // past expiry

// Alice's harvest succeeds and sets Pools[pool_id].last_update_block = E + 1 (> E)
assert_ok!(StakingRewards::harvest_rewards(RuntimeOrigin::signed(alice), pool_id, None));

// Bob's harvest now underflows in reward_per_token:
// last_block_reward_applicable(E) = E, E.ensure_sub(E + 1) -> Err(ArithmeticError::Underflow)
assert_err!(
    StakingRewards::harvest_rewards(RuntimeOrigin::signed(bob), pool_id, None),
    ArithmeticError::Underflow
);

// Bob's unstake also fails for the same reason, permanently locking his staked principal:
assert_err!(
    StakingRewards::unstake(RuntimeOrigin::signed(bob), pool_id, staked_amount, None),
    ArithmeticError::Underflow
);
```

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L523-526)
```rust
			// Always start by updating the pool rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L577-580)
```rust
			// Always start by updating the pool and staker rewards.
			let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
			let now = T::BlockNumberProvider::current_block_number();
			ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);
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
