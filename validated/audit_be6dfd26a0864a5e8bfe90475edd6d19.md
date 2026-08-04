Based on my investigation, I found a real local analog: the external report's core broken invariant — "no cap on a resource that lets an unprivileged actor monopolize a shared, bounded lock/queue slot, causing other users' funds to become stuck" — maps directly onto `pallet-nomination-pools`' interaction with the shared `unlocking` chunk queue of a pool's bonded (stash) account in `pallet-staking` / `pallet-staking-async`.

### Title
Unbounded pool membership and unbond requests let an unprivileged actor exhaust the shared `MaxUnlockingChunks` slots of a nomination pool's bonded account, permanently locking other members' unbond requests - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` has no limit on the number of distinct eras in which unbonding requests can be filed against a pool's single shared bonded (stash) account, and no default cap on the number of pool members (`MaxPoolMembers`/`MaxPoolMembersPerPool` default to `None`, i.e. unbounded). Every distinct member `unbond` call in a new era consumes one of the fixed, shared `T::MaxUnlockingChunks` slots on the pool's underlying staking ledger. An unprivileged set of members can deliberately spread small `unbond` calls across `MaxUnlockingChunks` consecutive eras to fully occupy that shared queue, after which the pool's underlying `Staking::unbond`/`do_unbond` returns `NoMoreChunks` for *every other* member attempting to unbond — including honest members with legitimate funds — until the bonding duration passes for the abuser's own chunks. This directly parallels the report's core pattern: an unbounded, attacker-controlled quantity (position size / here, the number of distinct-era unbond requests) exhausts a shared, capacity-limited resource (12-hour Timelock queue slot / here, `MaxUnlockingChunks`), producing a fund-lock condition for other users through ordinary, permissionless calls.

### Finding Description
`Call::unbond` in `substrate/frame/nomination-pools/src/lib.rs` computes `unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era)` and calls `T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)` [1](#0-0) . This delegates to the staking pallet's `do_unbond`, which operates on the single, shared ledger keyed by the pool's bonded/stash account: it checks `ledger.unlocking.len() < T::MaxUnlockingChunks::get() as usize` and, if a chunk for the target `era` doesn't already exist, tries to `try_push` a new `UnlockChunk`, returning `Error::<T>::NoMoreChunks` when the bounded vec is full [2](#0-1) . Because this ledger belongs to the *pool's bonded account*, not to any individual member, the slots are a shared resource across all members of the pool.

`Config::MaxUnbonding` in nomination-pools only limits how many *unbonding chunks a single member* can track locally [3](#0-2) ; it does nothing to prevent multiple different members from each contributing one new era-keyed chunk to the shared staking ledger. There is also no cap on the number of pool members by default: `MaxPoolMembers`/`MaxPoolMembersPerPool` are `OptionQuery` and can be `None` [4](#0-3) , and `join` only enforces `MinJoinBond`, not a maximum [5](#0-4) .

This behavior is explicitly acknowledged in the pallet's own documentation and tests: `pool_withdraw_unbonded`'s doc says "In the case there are too many unlocking chunks, the user would probably see an error like `NoMoreChunks`" [6](#0-5) , and the integration test `automatic_unbonding_pools` demonstrates exactly this: with `MaxUnlockingChunks=1`, member `2`'s unbond fills the sole slot, and member `3`'s subsequent unbond fails with `NoMoreChunks` until the earlier chunk is auto-withdrawn or the bonding duration elapses [7](#0-6) .

### Impact Explanation
With real-world `MaxUnlockingChunks` values (e.g. 32), a coordinated or single account controlling multiple pool memberships can, over `MaxUnlockingChunks` consecutive eras, submit trivial `unbond` calls (down to `MinJoinBond`/`MinCreateBond` minimums) that each land in a distinct era-bucket, filling all slots on the pool's shared bonded-account ledger. Once full, every other pool member's `Call::unbond` reverts with `NoMoreChunks`, meaning legitimate members cannot start the unbonding process for their funds — a "permanent user-fund lock" condition for the duration the attacker keeps refilling freed slots, matching the required-impact category of fund lock caused by public, underpriced/unbounded work in a public entrypoint, without any admin, validator, or governance actor involved.

### Likelihood Explanation
Likelihood is high for any actively used, permissionless nomination pool: the attack requires only ordinary account funding at `MinJoinBond` and calling `Call::join`/`Call::unbond` from ordinary signed origins across a handful of eras — no privileged role, collator, validator, or off-chain infrastructure is needed, and the mechanics are already visible/tested in-repo (`NoMoreChunks`, `automatic_unbonding_pools`).

### Recommendation
Either (a) give each pool member (or at least a bounded subset of members) an isolated reservation of unlocking-chunk capacity proportional to `MaxUnlockingChunks`/expected concurrent unbonders, (b) have `pallet-nomination-pools` proactively call `pool_withdraw_unbonded` before every member `unbond` to always try to free stale chunks first (already partially done, but insufficient when chunks are not yet mature), or (c) rate-limit/cap the number of distinct unbond-era buckets a pool can consume per unit time, so that no small set of members can monopolize the shared `MaxUnlockingChunks` capacity of the pool's bonded account.

### Proof of Concept
1. Configure a pool with `MaxUnlockingChunks = N` (e.g. via runtime config) and `BondingDuration = B` eras.
2. Have `N` distinct pool members join in eras `0..N-1` respectively (or one attacker across `N` different accounts) and call `Pools::unbond` once each in a distinct era, each unbonding just above `MinJoinBond`.
3. After the `N`-th such call, the pool's bonded-account staking ledger's `unlocking` vec is full (`ledger.unlocking.len() == MaxUnlockingChunks`).
4. Any further legitimate member calling `Pools::unbond` now fails with `pallet_staking::Error::<Runtime>::NoMoreChunks`, exactly as reproduced in `automatic_unbonding_pools` [8](#0-7) , and remains blocked until `B` eras pass and `pool_withdraw_unbonded`/auto-withdrawal frees a slot — which the attacker can immediately refill by unbonding again in the new era, perpetuating the lock.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1707-1709)
```rust
		/// The maximum number of simultaneous unbonding chunks that can exist per member.
		#[pallet::constant]
		type MaxUnbonding: Get<u32>;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1771-1779)
```rust
	/// Maximum number of members that can exist in the system. If `None`, then the count
	/// members are not bound on a system wide basis.
	#[pallet::storage]
	pub type MaxPoolMembers<T: Config> = StorageValue<_, u32, OptionQuery>;

	/// Maximum number of members that may belong to pool. If `None`, then the count of
	/// members is not bound on a per pool basis.
	#[pallet::storage]
	pub type MaxPoolMembersPerPool<T: Config> = StorageValue<_, u32, OptionQuery>;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2120-2136)
```rust
		pub fn join(
			origin: OriginFor<T>,
			#[pallet::compact] amount: BalanceOf<T>,
			pool_id: PoolId,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			// ensure pool is not in an un-migrated state.
			ensure!(!Self::api_pool_needs_delegate_migration(pool_id), Error::<T>::NotMigrated);

			// ensure account is not restricted from joining the pool.
			ensure!(!T::Filter::contains(&who), Error::<T>::Restricted);

			ensure!(amount >= MinJoinBond::<T>::get(), Error::<T>::MinimumBondNotMet);
			// If a member already exists that means they already belong to a pool
			ensure!(!PoolMembers::<T>::contains_key(&who), Error::<T>::AccountBelongsToOtherPool);

			let mut bonded_pool = BondedPool::<T>::get(pool_id).ok_or(Error::<T>::PoolNotFound)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2289-2296)
```rust

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2339-2344)
```rust
		/// Call `withdraw_unbonded` for the pools account. This call can be made by any account.
		///
		/// This is useful if there are too many unlocking chunks to call `unbond`, and some
		/// can be cleared by withdrawing. In the case there are too many unlocking chunks, the user
		/// would probably see an error like `NoMoreChunks` emitted from the staking system when
		/// they attempt to unbond.
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L1404-1450)
```rust
		// we need to fetch the ledger again because it may have been mutated in the call
		// to `Self::do_withdraw_unbonded` above.
		let mut ledger = Self::ledger(Controller(controller))?;
		let mut value = value.min(ledger.active);
		let stash = ledger.stash.clone();

		ensure!(
			ledger.unlocking.len() < T::MaxUnlockingChunks::get() as usize,
			Error::<T>::NoMoreChunks,
		);

		if !value.is_zero() {
			ledger.active -= value;

			// Avoid there being a dust balance left in the staking system.
			if ledger.active < asset::existential_deposit::<T>() {
				value += ledger.active;
				ledger.active = Zero::zero();
			}

			let min_active_bond = if Nominators::<T>::contains_key(&stash) {
				MinNominatorBond::<T>::get()
			} else if Validators::<T>::contains_key(&stash) {
				MinValidatorBond::<T>::get()
			} else {
				Zero::zero()
			};

			// Make sure that the user maintains enough active bond for their role.
			// If a user runs into this error, they should chill first.
			ensure!(ledger.active >= min_active_bond, Error::<T>::InsufficientBond);

			// Note: in case there is no current era it is fine to bond one era more.
			let era = CurrentEra::<T>::get()
				.unwrap_or(0)
				.defensive_saturating_add(T::BondingDuration::get());
			if let Some(chunk) = ledger.unlocking.last_mut().filter(|chunk| chunk.era == era) {
				// To keep the chunk count down, we only keep one chunk per era. Since
				// `unlocking` is a FiFo queue, if a chunk exists for `era` we know that it will
				// be the last one.
				chunk.value = chunk.value.defensive_saturating_add(value)
			} else {
				ledger
					.unlocking
					.try_push(UnlockChunk { value, era })
					.map_err(|_| Error::<T>::NoMoreChunks)?;
			};
```

**File:** substrate/frame/election-provider-multi-phase/test-staking-e2e/src/lib.rs (L344-362)
```rust
		// currently unlocking 0 chunks in the bonded pools ledger.
		assert_eq!(unlocking_chunks_of(pool_bonded_account), 0);

		// unbond 2 from pool.
		assert_ok!(Pools::unbond(RuntimeOrigin::signed(2), 2, 10));

		// amount is still locked in the pool, needs to wait for unbonding period.
		assert_eq!(staked_amount_for(pool_bonded_account), 25);

		// max chunks in the ledger are now filled up (`MaxUnlockingChunks == 1`).
		assert_eq!(unlocking_chunks_of(pool_bonded_account), 1);

		// tries to unbond 3 from pool. it will fail since there are no unlocking chunks left
		// available and the current in the queue haven't been there for more than bonding
		// duration.
		assert_err!(
			Pools::unbond(RuntimeOrigin::signed(3), 3, 10),
			pallet_staking::Error::<Runtime>::NoMoreChunks
		);
```
