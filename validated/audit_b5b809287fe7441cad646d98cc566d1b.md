This confirms the analog: `Pallet::unbond` in `pallet-nomination-pools` ( [1](#0-0) ) delegates directly to `T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)`, which forwards to the *same shared* underlying staking ledger for the pool's single bonded account ( [2](#0-1) ). That shared ledger is subject to the `MaxUnlockingChunks` cap enforced in `pallet-staking`'s `do_unbond`/`unbond` ( [3](#0-2)  and the staking-async equivalent at [4](#0-3) ), which fails with `Error::NoMoreChunks` once the bounded `unlocking` vec is full and none of the chunks have yet reached `BondingDuration` age.

### Title
Pool-wide `unbond` DoS via shared bonded-account `MaxUnlockingChunks` exhaustion in `pallet-nomination-pools` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` funnels every member's `unbond` call into a *single shared* staking ledger — the pool's bonded account — via `T::StakeAdapter::unbond`. That underlying staking pallet enforces a fixed, small `MaxUnlockingChunks` bound per account, exactly mirroring GMX's per-account `cooldownDuration`: repeated cheap actions on the shared account can keep the bound permanently saturated, denying the legitimate `unbond` operation for every other member of the pool.

### Finding Description
The pool `unbond` extrinsic is: `let (mut member, ...) = ...; ... T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;` [5](#0-4) . This routes into the generic `StakeStrategy::unbond` default impl, `Self::CoreStaking::unbond(&pool_account.0, amount)` [2](#0-1) , i.e. `pallet_staking::Pallet::unbond` acting on the pool's bonded account (not on the individual member's account).

In `pallet-staking`, `unbond`/`do_unbond` enforces: `ensure!(ledger.unlocking.len() < T::MaxUnlockingChunks::get() as usize, Error::<T>::NoMoreChunks);` and creates one chunk per era (merging same-era chunks only) [6](#0-5) . The `staking-async` pallet enforces the identical bound [7](#0-6) . Both pallets attempt an *implicit* `do_withdraw_unbonded` when the vec is already full, but that only frees chunks whose era has already passed `BondingDuration` [8](#0-7) .

Because *every* member of a nomination pool shares this one bonded account and its one `unlocking` `BoundedVec`, any unprivileged member (an attacker needs only to `join` the pool with a minimal, even dust, deposit — join is a public permissionless call) can call `Pools::unbond` with a small amount once per era. As documented in-repo, a full `MaxUnlockingChunks` queue causes subsequent `unbond` attempts by *any other member* to fail with `NoMoreChunks`, and the fix requires waiting out `BondingDuration`: “If there are too many unlocking chunks to call `unbond`... the user would probably see an error like `NoMoreChunks`” [9](#0-8) . Because the attacker refreshes one new chunk every era exactly as older ones expire, the queue can be kept perpetually at capacity — the same "attacker cheaply refreshes the shared cooldown/limit" primitive as the GMX report, except here the shared resource is the pool's bonded-account `unlocking` bound rather than a time-based cooldown.

`pool_withdraw_unbonded`, the suggested remedy, is a no-op against an actively-refilled queue since it can only release chunks that are already past `BondingDuration` [10](#0-9) .

### Impact Explanation
Every honest member of an affected pool becomes permanently unable to call `Pools::unbond` and thus cannot begin the withdrawal of their staked funds, effectively locking their stake in the pool for as long as the attacker keeps re-filling the shared chunk queue. This is a real fund-lock / permanent-DoS impact on the pool's core redemption path, aligned with the "permanent user-fund lock" and "public underpriced work" impact categories, since the attacker's only cost is one small `Pools::unbond` transaction (with dust amount) per era.

### Likelihood Explanation
The `join`, `bond_extra`, and `unbond` calls in `pallet-nomination-pools` are fully permissionless and require no privileged origin — any account can join a public pool with the existential minimum and unbond a dust amount every era. `MaxUnlockingChunks` is a small fixed bound (configured per-runtime, e.g. tied to `MaxUnlockingChunks`/`BondingDuration` as seen in `asset-hub-westend` config: `type MaxUnbonding = <Self as pallet_staking_async::Config>::MaxUnlockingChunks` [11](#0-10) ), so the number of eras an attacker must "race" to keep the queue full is bounded and cheap relative to the value locked for other members.

### Recommendation
Do not let a single member's `unbond` compete for the shared pool-account's finite `unlocking` chunk slots on a first-come basis. Options: reserve/pre-allocate unbonding capacity per member (e.g., track pool-level unbonding requests independent of `MaxUnlockingChunks` and batch-merge them before calling into `pallet-staking`), rate-limit or bond a minimum stake before a member can trigger a chunk-consuming unbond, or increase `MaxUnlockingChunks` dynamically / provide a queuing mechanism so legitimate withdrawal requests are guaranteed eventual processing regardless of other members' unbond frequency.

### Proof of Concept
1. Attacker calls `Pools::join(origin, existential_deposit, pool_id)` to become a low-stake member of a target pool.
2. Every era, attacker calls `Pools::unbond(origin, attacker_account, 1)` (dust amount), which forwards to `pallet_staking::unbond` on the pool's shared bonded account, pushing a new `UnlockChunk` for `era = current_era + bonding_duration` [12](#0-11) .
3. Once `ledger.unlocking.len() == MaxUnlockingChunks`, a legitimate member calling `Pools::unbond` triggers `pallet_staking::do_unbond`, which tries `do_withdraw_unbonded` first; since the attacker's chunks are continuously refreshed and not yet past `BondingDuration`, nothing is freed, and the `ensure!(... < MaxUnlockingChunks ...)` check fails, returning `Error::<T>::NoMoreChunks` [13](#0-12) .
4. The honest member's stake remains locked in the pool indefinitely as long as the attacker repeats step 2 every era, at negligible per-era transaction cost.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2257-2296)
```rust
		pub fn unbond(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			#[pallet::compact] unbonding_points: BalanceOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
			// ensure member is not in an un-migrated state.
			ensure!(
				!Self::api_member_needs_delegate_migration(member_account.clone()),
				Error::<T>::NotMigrated
			);

			let (mut member, mut bonded_pool, mut reward_pool) =
				Self::get_member_with_pools(&member_account)?;

			bonded_pool.ok_to_unbond_with(&who, &member_account, &member, unbonding_points)?;

			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2347-2367)
```rust
		pub fn pool_withdraw_unbonded(
			origin: OriginFor<T>,
			pool_id: PoolId,
			num_slashing_spans: u32,
		) -> DispatchResult {
			ensure_signed(origin)?;
			// ensure pool is not in an un-migrated state.
			ensure!(!Self::api_pool_needs_delegate_migration(pool_id), Error::<T>::NotMigrated);

			let pool = BondedPool::<T>::get(pool_id).ok_or(Error::<T>::PoolNotFound)?;

			// For now we only allow a pool to withdraw unbonded if its not destroying. If the pool
			// is destroying then `withdraw_unbonded` can be used.
			ensure!(pool.state != PoolState::Destroying, Error::<T>::NotDestroying);
			T::StakeAdapter::withdraw_unbonded(
				Pool::from(pool.bonded_account()),
				num_slashing_spans,
			)?;

			Ok(())
		}
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L172-175)
```rust
	/// See [`StakingInterface::unbond`].
	fn unbond(pool_account: Pool<Self::AccountId>, amount: Self::Balance) -> DispatchResult {
		Self::CoreStaking::unbond(&pool_account.0, amount)
	}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L1390-1450)
```rust
		let unlocking = Self::ledger(Controller(controller.clone())).map(|l| l.unlocking.len())?;

		// if there are no unlocking chunks available, try to withdraw chunks older than
		// `BondingDuration` to proceed with the unbonding.
		let maybe_withdraw_weight = {
			if unlocking == T::MaxUnlockingChunks::get() as usize {
				let real_num_slashing_spans =
					SlashingSpans::<T>::get(&controller).map_or(0, |s| s.iter().count());
				Some(Self::do_withdraw_unbonded(&controller, real_num_slashing_spans as u32)?)
			} else {
				None
			}
		};

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

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1964-2023)
```rust
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

				let is_nominator = Nominators::<T>::contains_key(&stash);

				let min_active_bond = if is_nominator {
					Self::min_nominator_bond()
				} else if Validators::<T>::contains_key(&stash) {
					Self::min_validator_bond()
				} else {
					// staker is chilled, no min bond.
					Zero::zero()
				};

				// Make sure that the user maintains enough active bond for their role.
				// If a user runs into this error, they should chill first.
				ensure!(ledger.active >= min_active_bond, Error::<T>::InsufficientBond);

				// Determine unbonding duration based on validator history.
				// If the account was a validator in recent eras (within BondingDuration), they must
				// wait the full BondingDuration even if they've switched to nominator role.
				// This prevents validators from avoiding slashing by switching roles and using the
				// shorter nominator unbonding period.
				let active_era = session_rotation::Rotator::<T>::active_era();
				let was_recent_validator = LastValidatorEra::<T>::get(&stash)
					.map(|last_era| active_era.saturating_sub(last_era) < T::BondingDuration::get())
					.unwrap_or(false);

				let unbond_duration = if was_recent_validator {
					// Use full bonding duration for recent validators
					T::BondingDuration::get()
				} else {
					// Use nominator bonding duration for pure nominators
					<Self as sp_staking::StakingInterface>::nominator_bonding_duration()
				};

				let era =
					session_rotation::Rotator::<T>::active_era().saturating_add(unbond_duration);
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

**File:** cumulus/parachains/runtimes/assets/asset-hub-westend/src/staking.rs (L522-528)
```rust
	type StakeAdapter =
		pallet_nomination_pools::adapter::DelegateStake<Self, Staking, DelegatedStaking>;
	// Buffer (4) + bonding duration (2).
	type MaxUnbondingPools = ConstU32<6>;
	type MaxMetadataLen = ConstU32<256>;
	// we use the same number of allowed unlocking chunks as with staking.
	type MaxUnbonding = <Self as pallet_staking_async::Config>::MaxUnlockingChunks;
```
