I found a strong local analog in `substrate/frame/asset-rewards/src/lib.rs`, whose own doc comment explicitly states it is modeled on Synthetix's `StakingRewards.sol` — the exact contract family cited in the external report. The pallet correctly avoids the "unfair first staker" variant of the bug (it always advances `last_update_block` on every action, unlike the Solidity contracts), but it introduces a different, more severe defect around the pool's `expiry_block` boundary that produces a permanent fund lock.

### Title
Post-expiry reward bookkeeping corrupts `last_update_block`, permanently bricking `unstake`/`harvest_rewards` for remaining stakers - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` implements a Synthetix-style JIT reward accounting scheme. `update_pool_rewards` unconditionally sets `last_update_block` to the current block, without capping it at the pool's `expiry_block`. Once any account calls `unstake` or `harvest_rewards` on a pool after its `expiry_block` has passed, `last_update_block` is pushed past `expiry_block`. Every subsequent reward computation for that pool then underflows in `reward_per_token`, causing `stake`, `unstake`, and `harvest_rewards` to revert with an arithmetic error for as long as `total_tokens_staked` remains nonzero — permanently freezing the remaining stakers' tokens (held under a `Freeze`) and unclaimed rewards, and also permanently blocking `cleanup_pool` (which requires an empty pool).

### Finding Description
`reward_per_token` computes elapsed blocks capped by expiry: [1](#0-0) 

`last_block_reward_applicable` caps at `expiry_block`: [2](#0-1) 

But `update_pool_rewards`, called after every reward computation, unconditionally sets `last_update_block` to the **current block number**, not to the expiry-capped value: [3](#0-2) 

`unstake` and `harvest_rewards` explicitly permit calls after expiry (`now > pool_info.expiry_block || caller == staker`), and a staker can always call these on their own behalf regardless of time: [4](#0-3) [5](#0-4) 

Sequence of the bug:
1. Pool has `expiry_block = E`, with `total_tokens_staked > 0`.
2. Block height passes `E`. Any staker calls `harvest_rewards` (or `unstake`) for themselves — this is normal, encouraged behavior, not privileged or malicious.
3. `reward_per_token` computes `rewardable_blocks_elapsed = last_block_reward_applicable(E) - last_update_block = E - last_update_block` (valid, since `last_update_block <= E` at this point).
4. `update_pool_rewards` then sets `new_pool_info.last_update_block = current_block_number()`, which is now `> E`. This poisons the pool's stored state.
5. Any later call to `stake`, `unstake`, or `harvest_rewards` on this pool (while `total_tokens_staked != 0`) re-enters `reward_per_token`, which computes `last_block_reward_applicable(E).ensure_sub(last_update_block)` = `E.ensure_sub(last_update_block)` where `last_update_block > E`. `ensure_sub` underflows and returns `Err(ArithmeticError)`, propagated via `?`, aborting the extrinsic.
6. Every remaining staker in the pool is now permanently unable to `unstake` (their tokens stay frozen forever) or `harvest_rewards` (unclaimed rewards become permanently unclaimable). `cleanup_pool` also requires `PoolStakers` to be empty, so the pool can never be cleaned up either.

This is the same root defect class as the external report — the code fails to consistently and correctly bound reward-time bookkeeping at a boundary condition (there: zero total supply; here: pool expiry) — but manifests as a hard state-corruption/DoS rather than a reward-misattribution.

### Impact Explanation
Once triggered, this is a permanent, irreversible user-fund lock: staked assets remain frozen under `FreezeReason::Staked` with no code path to remove the freeze, and accrued but unharvested rewards become permanently stuck in the pool's reward account. No admin or governance action can recover it, since `cleanup_pool` requires an empty `PoolStakers` set that can never be reached. This matches the "permanent user-fund... lock" and "runtime bugs that compromise intended behavior" categories in the impact gate.

### Likelihood Explanation
Likelihood is high: the trigger condition requires no malicious actor, no privileged role, and no unusual configuration — a single legitimate `harvest_rewards` or `unstake` call by any staker after their own pool's natural `expiry_block` passes is sufficient to poison the pool for everyone else still staked in it. Any long-lived pool with a fixed expiry will hit this in ordinary operation.

### Recommendation
Cap `last_update_block` at the reward-applicable block instead of the raw current block, e.g. in `update_pool_rewards` set `new_pool_info.last_update_block = Self::last_block_reward_applicable(pool_info.expiry_block)` rather than `T::BlockNumberProvider::current_block_number()`. This keeps `last_update_block <= expiry_block` for all pools past expiry, preventing the underflow in `reward_per_token` and allowing `unstake`/`harvest_rewards`/`cleanup_pool` to keep functioning after expiry.

### Proof of Concept
1. `create_pool` with `expiry: DispatchTime::At(E)`.
2. Staker A `stake`s tokens before block `E`.
3. Advance chain to block `E + 1` (or any block `> E`).
4. Staker A calls `harvest_rewards` (self-call, always permitted) — succeeds, but sets `Pools::<T>::get(pool_id).last_update_block = E + 1 > E`.
5. Advance one more block (or stay at same block) and call `harvest_rewards`/`unstake`/`stake` again for the same pool (any staker) — the call now fails with an `ArithmeticError` from `reward_per_token`'s `ensure_sub`, because `last_block_reward_applicable` returns `E` while `pool_info.last_update_block = E + 1`.
6. Every subsequent call to `stake`/`unstake`/`harvest_rewards` for this pool reverts identically as long as `total_tokens_staked != 0`; remaining stakers' frozen tokens and rewards are permanently unreachable.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-530)
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
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L568-585)
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
