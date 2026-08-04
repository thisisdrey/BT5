### Title
Permissionless dust `stake()` permanently blocks `cleanup_pool()`, locking admin's leftover reward funds forever - (File: substrate/frame/asset-rewards/src/lib.rs)

### Summary
`pallet-asset-rewards::cleanup_pool()` requires that a pool has zero entries in `PoolStakers` before the admin can reclaim the pool's remaining reward-asset balance and release the storage deposit. However, the public `stake()` extrinsic has no restriction preventing anyone from creating (and indefinitely keeping) a `PoolStakers` entry — even after the pool has expired. An unprivileged attacker can stake a dust amount of the staked asset and simply never call `unstake`/`harvest_rewards`, permanently keeping `PoolStakers::<T>::iter_key_prefix(pool_id)` non-empty. This blocks `cleanup_pool()` forever for that pool, permanently locking the admin's residual reward-asset funds in the pool's sub-account and the storage-deposit `Consideration` held for the pool.

### Finding Description
The relevant call flow:
- `Pallet::stake()` [1](#0-0) 
- `Pallet::cleanup_pool()` [2](#0-1) 

`stake()` is callable by any signed account for any existing pool and does not check `expiry_block` or require any admin permission; it simply freezes `amount` of the staked asset and inserts/updates a `PoolStakers` entry for the caller: [3](#0-2) 

`cleanup_pool()` is gated on the invariant that no stakers remain: [4](#0-3) 
```
let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);
```

Because `stake()` places no upper bound on how long a staker entry may persist, and because there is no incentive or requirement for a staker to ever unstake, any account holding even 1 unit of the staked asset can call `stake(pool_id, 1)` and never call `unstake`. The `PoolStakers` entry then persists indefinitely, since removal only happens inside `unstake`/`harvest_rewards` when the staker's `amount` and `rewards` both reach zero: [5](#0-4) 

This is structurally the same broken invariant as the reported `MultiRewards::_setRewardsDuration()` bug: a cheap, permissionless, "normal-usage" action (`addIncentives`/`stake`) advances or maintains a state condition (`periodFinish`/non-empty `PoolStakers`) that gates a privileged administrative operation (`_setRewardsDuration`/`cleanup_pool`), and the gating check has no mechanism to exclude dust/griefing actors. Unlike `set_pool_expiry_block`/`set_pool_reward_rate_per_block` (which are admin-gated and unaffected by this pattern), `cleanup_pool` depends on a *permissionless, externally-observable, externally-influenceable* precondition (`PoolStakers` emptiness), exactly mirroring the external report's `periodFinish` precondition on `_setRewardsDuration`.

Existing guards do not stop this path:
- `stake()` performs no expiry check, no minimum-stake enforcement tied to pool lifecycle, and no restriction preventing dust stakes purely to grief.
- `cleanup_pool()` only checks `pool_info.admin == who` and `stakers.is_none()`; it has no override or force-path for the admin to evict abandoned dust stakers.
- There is no time-based expiry of `PoolStakers` entries independent of an explicit `unstake` call.

### Impact Explanation
The pool's residual reward-asset balance (held in the pool's derived sub-account `pool_info.account`) and the storage-deposit `Consideration` recorded in `PoolCost` become permanently unrecoverable for that pool, since `cleanup_pool` is the only extrinsic that returns them to the admin and removes the `Pools`/`PoolCost` storage. This is a permanent fund lock affecting the pool admin/creator, matching the "permanent user-fund ... lock" impact category.

### Likelihood Explanation
High. Any account that already holds a non-zero balance of the pool's staked asset (which for public-facing incentive pools is generally by design permissionless/available to many holders) can execute this with a single `stake()` call of the smallest representable non-zero amount, at negligible cost (in most cases just transaction fees), and requires no special privilege, timing race, or governance/validator/relayer role — the same way `harvestVault()`/`addIncentives()` in the original report could be triggered through ordinary usage.

### Recommendation
- Decouple `cleanup_pool`'s liveness from an externally-griefable precondition. Options:
  1. Allow the pool admin to force-clear/evict abandoned staker entries with zero economic stake below a dust threshold (or entries whose `amount` is negligible and the pool has been expired for a sufficiently long grace period), before running `cleanup_pool`.
  2. Enforce a minimum stake amount (existential-deposit-like) for `stake()` so dust stakes are rejected outright.
  3. Allow `cleanup_pool` to proceed by force-unstaking/auto-settling all remaining stakers (paying out any owed rewards and returning frozen tokens) rather than requiring the caller to have done so beforehand.
- At minimum, add tests exercising the scenario where a staker deliberately never unstakes past pool expiry, to make this failure mode visible and enable an explicit fix decision.

### Proof of Concept
1. Admin calls `create_pool(staked_asset_id, reward_asset_id, reward_rate_per_block, expiry, admin)` to create `pool_id`.
2. Attacker (any account already holding ≥1 unit of `staked_asset_id`) calls `stake(pool_id, 1)`. This inserts `PoolStakers::<T>::insert(pool_id, attacker, ...)`.
3. Time advances past `expiry_block`; reward distribution ends.
4. Admin calls `cleanup_pool(pool_id)`.
5. `PoolStakers::<T>::iter_key_prefix(pool_id).next()` still returns `Some(attacker)` because the attacker never called `unstake`, so `cleanup_pool` fails with `Error::<T>::NonEmptyPool`.
6. The attacker never calls `unstake`. The admin has no way to force removal; the pool's residual reward-asset balance and storage deposit remain locked indefinitely. [2](#0-1) [3](#0-2)

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L548-554)
```rust
			staker_info.amount.ensure_sub_assign(amount)?;

			if staker_info.amount.is_zero() && staker_info.rewards.is_zero() {
				PoolStakers::<T>::remove(&pool_id, &staker);
			} else {
				PoolStakers::<T>::insert(&pool_id, &staker, staker_info);
			}
```

**File:** substrate/frame/asset-rewards/src/lib.rs (L696-729)
```rust
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
