## Title
Permanent Fund Lock in `pallet-asset-rewards` from Uncapped `last_update_block` After Pool Expiry - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards` implements an `ExpirableFarm`-like construct: a `PoolInfo.expiry_block` after which reward accrual stops, mirrored by the `reward_per_token()` helper that caps elapsed blocks at `expiry_block` via `last_block_reward_applicable()`. However, the companion function `update_pool_rewards()` unconditionally stamps `last_update_block` to the *current* block number instead of capping it to `expiry_block`. Once any state-mutating call (`stake`, `unstake`) is made after expiry — which any ordinary staker is free to do on their own behalf — the persisted `last_update_block` exceeds `expiry_block`. Every subsequent call to `reward_per_token()` then performs `expiry_block.ensure_sub(last_update_block)`, which underflows and errors out, permanently reverting `unstake`, `harvest_rewards`, `stake`, `set_pool_reward_rate_per_block`, and even the admin's own remediation call `set_pool_expiry_block` (which also calls `reward_per_token()` on the stale pool state before applying the new expiry).

### Finding Description
The pool's reward accounting keeps two pieces of state per `PoolInfo<...>`: `reward_per_token_stored` and `last_update_block` [1](#0-0) .

`reward_per_token()` correctly caps the reward-accrual window at `expiry_block` using `last_block_reward_applicable`: [2](#0-1) , and `last_block_reward_applicable` itself returns `min(now, expiry_block)`: [3](#0-2) .

But `update_pool_rewards()`, which persists the new checkpoint after every call, sets `last_update_block` to the *actual current block* rather than the capped value: [4](#0-3) .

`unstake` explicitly allows the staker to act on their own account at any time, independent of expiry: `ensure!(now > pool_info.expiry_block || caller == staker, BadOrigin);` [5](#0-4) , and then persists the pool state via `Pools::<T>::insert(pool_id, pool_info);` [6](#0-5) . `stake` behaves the same way, persisting the pool state unconditionally: [7](#0-6) .

Once one such call executes after `expiry_block` has passed, `PoolInfo.last_update_block` becomes strictly greater than `PoolInfo.expiry_block`. Any future call to `reward_per_token()` (invoked from `stake`, `unstake`, `harvest_rewards`, `set_pool_reward_rate_per_block`, and `set_pool_expiry_block`) computes `last_block_reward_applicable(expiry_block).ensure_sub(pool_info.last_update_block)`, i.e. `expiry_block - last_update_block`, which underflows because `last_update_block > expiry_block`. `ensure_sub` on the unsigned `BlockNumberFor<T>` type returns an `Err` on underflow, causing every subsequent extrinsic touching this pool to fail deterministically.

Critically, `set_pool_expiry_block` — the only apparent admin remedy — also calls `reward_per_token(&pool_info)` on the pool's existing (already-corrupted) state before applying any new expiry value: [8](#0-7) . Since this call uses the *old* `expiry_block` together with the already-inflated `last_update_block`, it fails with the same underflow, so even the pool admin cannot repair the pool once it is corrupted.

This is the same root defect as the reported `ExpirableFarm.sol` bug (state updates gated/mis-tracked around the expiry boundary), but here the consequence is worse: rather than merely under-accruing rewards, it produces a hard arithmetic error that permanently bricks every future interaction with the pool, locking any remaining staked tokens and unclaimed reward-asset balances in the pool account.

### Impact Explanation
Once triggered, `total_tokens_staked` for the pool can never be reduced via `unstake` again (it will always fail), and `harvest_rewards` will always fail as well, since both routes call `reward_per_token()`. `cleanup_pool` requires the pool to have no remaining stakers (`ensure!(stakers.is_none(), Error::<T>::NonEmptyPool)`), so the admin also cannot force-close and refund the pool while stakers remain locked in. This satisfies the "permanent user-fund or bridge-state lock" impact category: staked asset balances and reward-asset balances held by the pool's sub-account become permanently unreachable by ordinary means.

### Likelihood Explanation
No privileged actor, malicious peer, or governance intervention is required. Any staker can simply call `unstake` on their own position (`caller == staker` bypasses the expiry check) after `expiry_block` has passed but while other stakers still have `total_tokens_staked > 0`; this legitimate, expected action alone advances `last_update_block` past `expiry_block` and persists it. Any live pool that outlives its `expiry_block` while still holding multiple stakers is exposed to this by ordinary usage, making the likelihood high for any deployed instance of this pallet with a bounded farm lifetime.

### Recommendation
Cap `last_update_block` to `last_block_reward_applicable(pool_info.expiry_block)` in `update_pool_rewards()` instead of `T::BlockNumberProvider::current_block_number()`, mirroring the cap already applied in `reward_per_token()`. This ensures `last_update_block` never exceeds `expiry_block`, eliminating the underflow in subsequent `ensure_sub` calls and matching the semantics that `isFarmOpen()`-style guards intend (no further accrual past expiry, but also no corrupted checkpoint state).

### Proof of Concept
1. Create a pool with `expiry_block = 25`, two stakers `staker1` and `staker2`, both staking non-zero amounts before expiry (as in the existing `integration` test) [9](#0-8) .
2. Advance the block number past `expiry_block` (e.g., to block 30) without calling `set_pool_expiry_block`.
3. `staker1` calls `unstake` for their own stake (`caller == staker`, allowed regardless of expiry) — this persists `pool_info.last_update_block = 30` while `expiry_block` remains `25`, via the `Pools::<T>::insert` in `unstake` [6](#0-5) .
4. `staker2`, who still has staked tokens, calls `unstake` or `harvest_rewards`. `reward_per_token()` computes `last_block_reward_applicable(25).ensure_sub(30)` = `25 - 30`, which underflows and returns `Err`, causing the call to fail [10](#0-9) .
5. The pool admin attempts `set_pool_expiry_block` to fix the pool; this also fails because it first calls `reward_per_token(&pool_info)` on the stale, corrupted `pool_info` [11](#0-10) .
6. `staker2`'s stake and any pending rewards are now permanently locked in the pool's sub-account, with no available extrinsic path to recover them.

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L160-164)
```rust
	total_tokens_staked: Balance,
	/// Total rewards accumulated per token, up to the `last_update_block`.
	reward_per_token_stored: Balance,
	/// Last block number the pool was updated.
	last_update_block: BlockNumber,
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L476-492)
```rust
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
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L519-530)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L543-545)
```rust
			// Update Pools.
			pool_info.total_tokens_staked.ensure_sub_assign(amount)?;
			Pools::<T>::insert(pool_id, pool_info);
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L940-958)
```rust
	fn set_pool_expiry_block(
		admin: &T::AccountId,
		pool_id: PoolId,
		new_expiry: DispatchTime<BlockNumberFor<T>>,
	) -> DispatchResult {
		let now = T::BlockNumberProvider::current_block_number();
		let new_expiry_block = new_expiry.evaluate(now);
		ensure!(new_expiry_block > now, Error::<T>::ExpiryBlockMustBeInTheFuture);

		let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
		ensure!(pool_info.admin == *admin, BadOrigin);
		ensure!(new_expiry_block > pool_info.expiry_block, Error::<T>::ExpiryCut);

		// Always start by updating the pool rewards.
		let reward_per_token = Self::reward_per_token(&pool_info)?;
		let mut pool_info = Self::update_pool_rewards(&pool_info, reward_per_token)?;

		pool_info.expiry_block = new_expiry_block;
		Pools::<T>::insert(pool_id, pool_info);
```

**File:** substrate/frame/asset-rewards/src/tests.rs (L1293-1349)
```rust
#[test]
fn integration() {
	new_test_ext().execute_with(|| {
		let admin = 1;
		let staker1 = 10u128;
		let staker2 = 20;
		let staked_asset_id = NativeOrWithId::<u32>::WithId(1);
		let reward_asset_id = NativeOrWithId::<u32>::Native;
		let reward_rate_per_block = 100;
		let lifetime = 24u64.into();
		System::set_block_number(1);
		assert_ok!(StakingRewards::create_pool(
			RuntimeOrigin::root(),
			Box::new(staked_asset_id.clone()),
			Box::new(reward_asset_id.clone()),
			reward_rate_per_block,
			DispatchTime::After(lifetime),
			Some(admin)
		));
		let pool_id = 0;

		// Block 7: Staker 1 stakes 100 tokens.
		System::set_block_number(7);
		assert_ok!(StakingRewards::stake(RuntimeOrigin::signed(staker1), pool_id, 100));
		// At this point
		// - Staker 1 has earned 0 tokens.
		// - Staker 1 is earning 100 tokens per block.

		// Check that Staker 1 has earned 0 tokens.
		assert_hypothetically_earned(staker1, 0, pool_id, reward_asset_id.clone());

		// Block 9: Staker 2 stakes 100 tokens.
		System::set_block_number(9);
		assert_ok!(StakingRewards::stake(RuntimeOrigin::signed(staker2), pool_id, 100));
		// At this point
		// - Staker 1 has earned 200 (100*2) tokens.
		// - Staker 2 has earned 0 tokens.
		// - Staker 1 is earning 50 tokens per block.
		// - Staker 2 is earning 50 tokens per block.

		// Check that Staker 1 has earned 200 tokens and Staker 2 has earned 0 tokens.
		assert_hypothetically_earned(staker1, 200, pool_id, reward_asset_id.clone());
		assert_hypothetically_earned(staker2, 0, pool_id, reward_asset_id.clone());

		// Block 12: Staker 1 stakes an additional 100 tokens.
		System::set_block_number(12);
		assert_ok!(StakingRewards::stake(RuntimeOrigin::signed(staker1), pool_id, 100));
		// At this point
		// - Staker 1 has earned 350 (200 + (50 * 3)) tokens.
		// - Staker 2 has earned 150 (50 * 3) tokens.
		// - Staker 1 is earning 66.66 tokens per block.
		// - Staker 2 is earning 33.33 tokens per block.

		// Check that Staker 1 has earned 350 tokens and Staker 2 has earned 150 tokens.
		assert_hypothetically_earned(staker1, 350, pool_id, reward_asset_id.clone());
		assert_hypothetically_earned(staker2, 150, pool_id, reward_asset_id.clone());

```
