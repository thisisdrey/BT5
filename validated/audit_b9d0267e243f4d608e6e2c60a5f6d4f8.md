### Title
`update_pool_rewards` fails to cap `last_update_block` at `expiry_block`, permanently bricking expired reward pools and locking staked funds - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
The `pallet-asset-rewards` pool lifecycle relies on an expiration invariant (`expiry_block`) to stop reward accrual, similar in spirit to the license-terms expiration in the external report. However, the pallet never enforces that its own internal bookkeeping field (`last_update_block`) stays bounded by `expiry_block` once a pool has expired. Any two public calls (`stake`, `unstake`, `harvest_rewards`) made after expiry cause `last_update_block` to exceed `expiry_block`, after which every subsequent reward computation reverts with an arithmetic underflow, permanently disabling `stake`/`unstake`/`harvest_rewards` for that pool and freezing all staked tokens in it.

### Finding Description
`reward_per_token` computes the elapsed rewardable blocks as: [1](#0-0) 

using `last_block_reward_applicable(pool_info.expiry_block)`, which is capped at `expiry_block`: [2](#0-1) 

This subtraction (`expiry_block.ensure_sub(last_update_block)`) only succeeds while `last_update_block <= expiry_block`. However, `update_pool_rewards` unconditionally sets `last_update_block` to the *current* block number, without capping it to `expiry_block`: [3](#0-2) 

`stake` never checks whether the pool has already expired before calling this update path: [4](#0-3) 

`unstake` and `harvest_rewards` explicitly permit calls after expiry (by anyone, not just the staker): [5](#0-4) [6](#0-5) 

Sequence of events:
1. A pool has non-zero `total_tokens_staked` and reaches `expiry_block`.
2. Any account calls `unstake` or `harvest_rewards` once after expiry (this is explicitly allowed for any caller, since `now > pool_info.expiry_block`). `update_pool_rewards` succeeds (because at this point `last_update_block <= expiry_block` still) but sets `last_update_block = now` (a block number greater than `expiry_block`).
3. Any subsequent call to `stake`, `unstake`, or `harvest_rewards` on this pool calls `reward_per_token()`, which computes `last_block_reward_applicable(expiry_block) = expiry_block` and then executes `expiry_block.ensure_sub(last_update_block)`, which underflows and returns `Err`, because `last_update_block > expiry_block`.
4. This error propagates through `update_pool_and_staker_rewards` via the `?` operator in `stake`/`unstake`/`harvest_rewards`, causing every future call on this pool to permanently fail.

Because `unstake` is the only extrinsic that removes the `AssetsFreezer` freeze on staked tokens, and it can never succeed again once this state is reached, all staked funds in the pool become permanently locked. No admin/privileged action other than `set_pool_expiry_block` (which requires `new_expiry_block > last_update_block`) can restore the pool, and this is not guaranteed to be available or exercised — the root cause is an unprivileged, permissionless sequence of two ordinary public transactions.

### Impact Explanation
This is a permanent user-fund lock condition triggered purely by unprivileged public calls (`stake`/`unstake`/`harvest_rewards`), matching "permanent user-fund ... lock" and "message/queue/payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot categories. Once triggered, the reward pool's staked tokens (frozen via `T::AssetsFreezer`) can no longer be withdrawn by their owners through the pallet's own extrinsics, and no reward can be harvested, causing loss of access to funds.

### Likelihood Explanation
The trigger requires no privileged role: any account can call `harvest_rewards`/`unstake` on any staker's behalf once `now > expiry_block` (this is explicitly permitted by the `caller == staker` bypass condition), and any second such call thereafter reliably corrupts pool state. Every real-world reward pool eventually reaches its `expiry_block`, making this reachable in ordinary pool lifecycle without any adversarial assumptions (no malicious validator, relayer, or governance actor required).

### Recommendation
Cap `last_update_block` at `expiry_block` in `update_pool_rewards` (i.e., use `last_block_reward_applicable(pool_info.expiry_block)` instead of the raw current block number) so the invariant `last_update_block <= expiry_block` always holds once a pool is expired, preventing the underflow and allowing `unstake`/`harvest_rewards` to keep functioning for cleanup after expiration.

### Proof of Concept
```rust
// 1. Admin creates a pool with expiry_block = E, staker stakes tokens before E.
StakingRewards::create_pool(admin_origin, staked_asset, reward_asset, rate, DispatchTime::At(E), Some(admin))?;
StakingRewards::stake(RuntimeOrigin::signed(staker), pool_id, amount)?;

// 2. Advance chain past expiry.
System::set_block_number(E + 10);

// 3. First post-expiry call succeeds, but sets last_update_block = E + 10 (> E).
assert_ok!(StakingRewards::harvest_rewards(RuntimeOrigin::signed(staker), pool_id, None));

// 4. Advance further.
System::set_block_number(E + 20);

// 5. Any subsequent call now fails permanently due to underflow in reward_per_token():
//    expiry_block(E).ensure_sub(last_update_block(E+10)) -> Err
assert_err!(StakingRewards::unstake(RuntimeOrigin::signed(staker), pool_id, amount, None), /* arithmetic error */);
assert_err!(StakingRewards::harvest_rewards(RuntimeOrigin::signed(staker), pool_id, None), /* arithmetic error */);
// staker's frozen tokens can never be unstaked from this pool again.
```

### Citations

**File:** substrate/frame/asset-rewards/src/lib.rs (L472-502)
```rust
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

**File:** substrate/frame/asset-rewards/src/lib.rs (L569-585)
```rust
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
