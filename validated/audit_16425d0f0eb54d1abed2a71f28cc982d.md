## Analysis

The reported THENA `GaugeExtraRewarder` bug is a "reward-rate does not stop accruing past distribution end" bug caused by unconditionally setting `lastRewardTime = block.timestamp` instead of capping it to the distribution end (`periodFinish`). The exact same broken invariant exists in `pallet-asset-rewards`.

### Root cause

`last_block_reward_applicable` correctly caps "now" to the pool's `expiry_block`: [1](#0-0) 

and `reward_per_token` uses that capped value to compute elapsed reward blocks: [2](#0-1) 

However, `update_pool_rewards`, which persists `last_update_block` for the next call, stores the **raw, uncapped** current block instead of the capped value: [3](#0-2) 

This is line-for-line the same defect as THENA's `lastRewardTime = block.timestamp` instead of `lastRewardTime = min(block.timestamp, periodFinish)`.

### Consequence

`update_pool_and_staker_rewards` (called at the top of `stake`, `unstake`, and `harvest_rewards`) is pure/side-effect-free; only the caller commits `last_update_block` to storage: [4](#0-3) .

- **Call #1 after expiry** (any `stake`/`unstake`/`harvest_rewards` on an expired pool, which is explicitly permitted for non-owners after expiry via `now > pool_info.expiry_block || caller == staker`): [5](#0-4) [6](#0-5)  — this call succeeds, computing rewards correctly up to `expiry_block`, but then stores `last_update_block = current_block_number()`, which is now **greater than** `expiry_block`.
- **Any subsequent call** to `stake`/`unstake`/`harvest_rewards`/`set_pool_reward_rate_per_block`/`set_pool_expiry_block` on that pool recomputes `reward_per_token`: `last_block_reward_applicable(expiry_block)` returns `expiry_block` (capped), but `pool_info.last_update_block` is now `> expiry_block`. The subtraction `expiry_block.ensure_sub(last_update_block)` underflows, and `ensure_sub`'s `?` propagates an `ArithmeticError`, aborting the extrinsic.

Because this path is unconditionally hit by every staker-facing entry point before any staked-token transfer occurs, once one interaction happens after expiry, **every future `unstake`/`harvest_rewards` call on that pool reverts permanently** — no privileged actor, governance, or malicious peer is needed; it happens from ordinary post-expiry usage. Stakers can never unfreeze their staked assets or claim already-accrued rewards from that pool again, since `unstake` also fails via the same code path.

### Title
Incorrect `last_update_block` persistence in `pallet-asset-rewards` permanently bricks pool withdrawals after expiry - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`Pallet::update_pool_rewards` stores the raw current block number into `PoolInfo::last_update_block` instead of the expiry-capped value used by `reward_per_token`/`last_block_reward_applicable`. After a pool's `expiry_block` has passed and any staker interaction occurs, this desynchronization causes an arithmetic underflow on every subsequent `stake`, `unstake`, or `harvest_rewards` call for that pool, permanently freezing all remaining staked tokens and unclaimed rewards.

### Finding Description
`reward_per_token` (lines 787-810) computes rewardable blocks as `last_block_reward_applicable(expiry_block).ensure_sub(last_update_block)`, where `last_block_reward_applicable` caps "now" at `expiry_block` (lines 826-833). But `update_pool_rewards` (lines 775-784), which is the only writer of `last_update_block`, sets it to the uncapped `T::BlockNumberProvider::current_block_number()`. Once a call happens after `expiry_block`, `last_update_block` becomes strictly greater than `expiry_block`; on the next call the capped "now" (`expiry_block`) minus `last_update_block` underflows, and `EnsureSub` returns an `ArithmeticError`, aborting the whole extrinsic before any transfer/unfreeze logic runs.

### Impact Explanation
This meets the "permanent user-fund lock" bar: staked assets remain frozen via `T::AssetsFreezer` and accrued reward-asset balances become permanently unclaimable, since `unstake` (which removes the freeze) and `harvest_rewards` (which pays out rewards) both go through the same broken `update_pool_and_staker_rewards`/`reward_per_token` path and will revert for every user of that pool once triggered.

### Likelihood Explanation
High. No admin/governance/relayer/malicious actor is required — the bug is triggered by completely ordinary use: any account calling `unstake` or `harvest_rewards` on an expired pool (explicitly allowed for any caller once `now > pool_info.expiry_block`) suffices to corrupt `last_update_block`, after which the pool is bricked for everyone.

### Recommendation
In `update_pool_rewards`, set `last_update_block` to `Self::last_block_reward_applicable(pool_info.expiry_block)` instead of the raw `current_block_number()`, mirroring the fix in the referenced THENA PR that capped `lastRewardTime` at the distribution end.

### Proof of Concept
1. `create_pool` with `expiry_block = E`, `reward_rate_per_block > 0`, staker `A` stakes `amount > 0` before `E`.
2. Advance chain to block `E + 1`. Any account calls `unstake(pool_id, small_amount, Some(A))` (allowed since `now > expiry_block`): this succeeds, and internally sets `Pools::<T>::get(pool_id).last_update_block = E + 1` via `update_pool_rewards`.
3. Advance chain to block `E + 2`. Call `harvest_rewards(pool_id, Some(A))` or `unstake` again: `reward_per_token` computes `last_block_reward_applicable(E) = E`, then `E.ensure_sub(E + 1)` underflows, returning `ArithmeticError` and aborting the extrinsic.
4. From this point forward, all `stake`, `unstake`, and `harvest_rewards` calls on `pool_id` fail identically, permanently locking staker `A`'s remaining staked tokens and any accrued-but-unclaimed rewards.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L524-530)
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
