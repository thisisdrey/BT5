### Title
Permissionless griefing of `cleanup_pool` permanently locks pool deposit and reward funds - (File: `substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`pallet-asset-rewards`'s `cleanup_pool` extrinsic refuses to run if the pool has any staker on record, exactly mirroring the `YRizStrategy._checkPoolsWithBalanceAreIncluded` pattern where a non-zero balance blocks a state-changing action. Because `stake` is a fully permissionless, unguarded-by-expiry extrinsic, any unprivileged account can keep (or repeatedly re-create) a dust stake entry in `PoolStakers`, permanently preventing `cleanup_pool` from ever succeeding — locking the pool's storage deposit and any un-harvested reward-asset balance in the pool account forever.

### Finding Description
`cleanup_pool` requires the pool to have zero stakers before it will refund the storage deposit and return leftover reward tokens to the admin: [1](#0-0) 

```
pub fn cleanup_pool(origin: OriginFor<T>, pool_id: PoolId) -> DispatchResult {
    let who = ensure_signed(origin)?;
    let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
    ensure!(pool_info.admin == who, BadOrigin);
    let stakers = PoolStakers::<T>::iter_key_prefix(pool_id).next();
    ensure!(stakers.is_none(), Error::<T>::NonEmptyPool);
    ...
    Pools::<T>::remove(pool_id);
    ...
}
```

This check enforces the same invariant as the audited bug: "no removal/cleanup while a resource still has a balance/entry." In the audited contract, an attacker could front-run `setFullPoolDistribution` with a token transfer to keep a pool's balance non-zero, blocking the whole distribution update. Here, the analogous entry point is `stake`, which is completely permissionless and is **not gated by pool expiry**: [2](#0-1) 

```
pub fn stake(origin: OriginFor<T>, pool_id: PoolId, amount: T::Balance) -> DispatchResult {
    let staker = ensure_signed(origin)?;
    let pool_info = Pools::<T>::get(pool_id).ok_or(Error::<T>::NonExistentPool)?;
    ...
    T::AssetsFreezer::increase_frozen(...)?;
    pool_info.total_tokens_staked.ensure_add_assign(amount)?;
    Pools::<T>::insert(pool_id, pool_info);
    staker_info.amount.ensure_add_assign(amount)?;
    PoolStakers::<T>::insert(pool_id, &staker, staker_info);
    ...
}
```

Unlike `unstake` and `harvest_rewards`, which explicitly check `now > pool_info.expiry_block || caller == staker` to allow forced third-party settlement after expiry, `stake` has no such window restriction — a caller can (re)stake into a pool at any time, including long after the pool has expired and stopped accruing rewards.

`unstake` and `harvest_rewards` do allow *anyone* to force-unstake/harvest on behalf of a griefer's account after expiry: [3](#0-2) 

but this only clears `PoolStakers` momentarily. Because `stake` is permissionless and unrestricted by time, the same or a different unprivileged account can immediately re-insert a `PoolStakers` entry with a trivial (e.g. minimal freezable) amount right after being force-unstaked, re-arming the `NonEmptyPool` guard in `cleanup_pool` indefinitely. This is the exact functional analog of "any user may simply transfer a single token every time they detect the balance is zero" from the external report — no front-running or privileged role is required, only the ability to repeatedly call a public, unguarded extrinsic.

### Impact Explanation
As long as `PoolStakers` for a pool is non-empty, `cleanup_pool` cannot execute, which means:
- The storage-cost `Consideration` taken at pool creation (`PoolCost`) is never released/refunded.
- Any reward-asset balance remaining in the pool's dedicated account (deposited via `deposit_reward_tokens` or left over after expiry) can never be swept back to the admin, since `cleanup_pool` is the only path that transfers the pool's `reducible_balance` back to `pool_info.admin`.

This constitutes a permanent lock of pool-associated funds (deposit + reward-asset balance) caused entirely by an unprivileged third party repeatedly calling a public extrinsic, matching the "permanent user-fund lock" impact class.

### Likelihood Explanation
High. The attack requires only:
1. Holding a minimal amount of the staked asset (attacker's own asset, not privileged).
2. Calling `stake(pool_id, small_amount)` once, and repeating it forever/whenever someone tries to unstake/cleanup on their behalf.

No governance, admin, validator, collator, or malicious-peer assumption is required; it is a straightforward repeatable call to a public extrinsic with no cooldown, minimum-stake floor, or expiry gating.

### Recommendation
- Gate `stake` so it cannot be called (or is rejected) once `now > pool_info.expiry_block`, consistent with the restriction already applied to `unstake`/`harvest_rewards`.
- Alternatively/additionally, change `cleanup_pool` to force-unstake and force-harvest all remaining `PoolStakers` (analogous to the report's suggested fix of "automated withdrawal ... rather than a reversion") instead of reverting with `NonEmptyPool`, so that a hostile actor cannot indefinitely veto cleanup by maintaining a non-zero stake.

### Proof of Concept
1. Admin creates a pool via `create_pool` with an expiry block `E` and deposits reward tokens via `deposit_reward_tokens`.
2. Attacker calls `stake(pool_id, 1)` (any nonzero amount of the staked asset they hold) — `PoolStakers` now has one entry.
3. Time passes; pool expires (`now > E`).
4. Admin (or anyone) calls `unstake(pool_id, amount, Some(attacker))` to try to clear the attacker's entry, followed by `cleanup_pool`.
5. Before/immediately after step 4, attacker (or any other account) calls `stake(pool_id, 1)` again — since `stake` has no expiry check, this succeeds and re-populates `PoolStakers`.
6. `cleanup_pool` now fails again with `Error::<T>::NonEmptyPool`, and can be blocked indefinitely by repeating step 5, permanently trapping the pool's deposit and reward-asset balance.

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

**File:** substrate/frame/asset-rewards/src/lib.rs (L513-533)
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

			// Check the staker has enough staked tokens.
			ensure!(staker_info.amount >= amount, Error::<T>::NotEnoughTokens);
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
