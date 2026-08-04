## Local Analog Found: Nomination Pool member-slot exhaustion via mass join + self-unbond (griefing DoS)

### Title
Pool member-count DoS: attacker can fill a pool to `MaxPoolMembersPerPool`, self-unbond to zero active stake, and block new joiners indefinitely — ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The external report describes an attacker who fills a capacity-limited pool with many minimum-size deposits, then immediately withdraws to drive the pool's effective active stake to zero while keeping the slot list full, permanently blocking new deposits until the attacker chooses otherwise. The same primitive exists in `pallet-nomination-pools`: `member_counter` is incremented on `join`/`bond_extra` but is only decremented in `withdraw_unbonded`, which for ordinary members is a strictly *permissioned* call (the member themselves must call it). Between `unbond` and `withdraw_unbonded`, a member occupies a pool slot while contributing zero active points/stake.

### Finding Description
`BondedPool::try_inc_members` enforces `MaxPoolMembersPerPool` before allowing a `join`: [1](#0-0) 

`join` calls `try_inc_members` and inserts the new `PoolMember`: [2](#0-1) 

When a member calls `unbond`, their active `points` are dissolved from the `BondedPool` (reducing the pool's effective bonded/active stake), but the member is **not removed** from `PoolMembers` and `member_counter` is **not decremented**: [3](#0-2) 

The member slot (and the `MaxPoolMembersPerPool` accounting) is only freed in `withdraw_unbonded`, via `dec_members()`, and only once all of that member's `unbonding_eras` chunks have matured past the bonding duration: [4](#0-3) 

Critically, `withdraw_unbonded` for a normal (non-depositor) member is **permissioned** — the caller must be the member themselves, except in narrow permissionless cases (pool destroying, pool blocked + kicker, or depositor-as-sole-remaining-member): [5](#0-4) [6](#0-5) 

This exactly mirrors the reported bug-class: fill a capacity-bounded structure with minimum-sized entries, drive the aggregate contribution to (near) zero via a self-service withdraw path, and leave the slot list "full" so no new legitimate participants can join — with the only unlock path being a voluntary action by the attacker (`withdraw_unbonded`), which they can simply withhold indefinitely.

### Impact Explanation
An attacker (or any single actor controlling many accounts) can:
1. `join` a target pool with `MinJoinBond` from up to `MaxPoolMembersPerPool - 1` distinct accounts, saturating `member_counter`.
2. `unbond` the full amount from every one of those accounts, which is fully permissioned to the caller (their own points), driving `BondedPool.points` (and thus `active_stake`) down to essentially just the depositor's minimum bond.
3. Never call `withdraw_unbonded` on any of those accounts (that call is gated to be caller-permissioned for regular members), leaving `member_counter` pinned at the max.

Result: the pool is permanently (until the attacker relents) unable to accept new members via `join` (`Error::MaxPoolMembers`), while its productive bonded/staked value is reduced to near zero — a denial-of-service on that pool's growth and staking utility, degrading the pool's contribution to network security/staking without requiring any privileged role, malicious validator, or off-chain assumption.

### Likelihood Explanation
Low-to-medium: the attack costs only `MinJoinBond * (MaxPoolMembersPerPool - 1)`, entirely recoverable later by calling `withdraw_unbonded` after the bonding duration (funds are not lost, only temporarily locked in unbonding). The attacker gains nothing directly but can grief a competitor's or public pool's growth capacity — same low-likelihood/no-direct-profit profile as the original report.

### Recommendation
Decouple slot/member-count accounting from full withdrawal completion — e.g., decrement `member_counter` (or otherwise stop counting a member against `MaxPoolMembersPerPool`) as soon as a member's `active_points` reach zero via `unbond`, rather than waiting for the permissioned `withdraw_unbonded` call. Alternatively, allow permissionless `withdraw_unbonded` once a member has zero active points and matured unbonding chunks, so slots can be reclaimed by anyone once eligible.

### Proof of Concept
1. Set/observe a pool's `MaxPoolMembersPerPool` (e.g. 3 in test defaults, `substrate/frame/nomination-pools/src/tests.rs:983`).
2. From `MaxPoolMembersPerPool - 1` distinct sybil accounts, call `Pools::join(origin, MinJoinBond_amount, pool_id)` until the pool is at capacity — see `join_max_member_limits_are_respected` test pattern [7](#0-6) .
3. From each of those accounts, call `Pools::unbond(origin, member_account, unbonding_points)` for the full active points — permitted per-account since `is_full_unbond` is true and caller == target (`ok_to_unbond_with`) [8](#0-7) .
4. Observe `BondedPool.points` collapse toward the depositor's minimum while `member_counter` remains at `MaxPoolMembersPerPool`.
5. Any new account attempting `Pools::join` on this pool now fails with `Error::MaxPoolMembers` (see check in `try_inc_members`) even though the pool's real staked value is near zero.
6. The attacker withholds calling `withdraw_unbonded` on their own accounts indefinitely, keeping the pool DoS'd; funds return to them only once/if they choose to call it after the bonding duration elapses.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1101-1115)
```rust
	/// Increment the member counter. Ensures that the pool and system member limits are
	/// respected.
	fn try_inc_members(&mut self) -> Result<(), DispatchError> {
		ensure!(
			MaxPoolMembersPerPool::<T>::get()
				.map_or(true, |max_per_pool| self.member_counter < max_per_pool),
			Error::<T>::MaxPoolMembers
		);
		ensure!(
			MaxPoolMembers::<T>::get().map_or(true, |max| PoolMembers::<T>::count() < max),
			Error::<T>::MaxPoolMembers
		);
		self.member_counter = self.member_counter.checked_add(1).ok_or(Error::<T>::OverflowRisk)?;
		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1217-1289)
```rust
	fn ok_to_unbond_with(
		&self,
		caller: &T::AccountId,
		target_account: &T::AccountId,
		target_member: &PoolMember<T>,
		unbonding_points: BalanceOf<T>,
	) -> Result<(), DispatchError> {
		let is_permissioned = caller == target_account;
		let is_depositor = *target_account == self.roles.depositor;
		let is_full_unbond = unbonding_points == target_member.active_points();

		let balance_after_unbond = {
			let new_depositor_points =
				target_member.active_points().saturating_sub(unbonding_points);
			let mut target_member_after_unbond = (*target_member).clone();
			target_member_after_unbond.points = new_depositor_points;
			target_member_after_unbond.active_balance()
		};

		// any partial unbonding is only ever allowed if this unbond is permissioned.
		ensure!(
			is_permissioned || is_full_unbond,
			Error::<T>::PartialUnbondNotAllowedPermissionlessly
		);

		// any unbond must comply with the balance condition:
		ensure!(
			is_full_unbond ||
				balance_after_unbond >=
					if is_depositor {
						Pallet::<T>::depositor_min_bond()
					} else {
						MinJoinBond::<T>::get()
					},
			Error::<T>::MinimumBondNotMet
		);

		// additional checks:
		match (is_permissioned, is_depositor) {
			(true, false) => (),
			(true, true) => {
				// permission depositor unbond: if destroying and pool is empty, always allowed,
				// with no additional limits.
				if self.is_destroying_and_only_depositor(target_member.active_points()) {
					// everything good, let them unbond anything.
				} else {
					// depositor cannot fully unbond yet.
					ensure!(!is_full_unbond, Error::<T>::MinimumBondNotMet);
				}
			},
			(false, false) => {
				// If the pool is blocked, then an admin with kicking permissions can remove a
				// member. If the pool is being destroyed, anyone can remove a member
				debug_assert!(is_full_unbond);
				ensure!(
					self.can_kick(caller) || self.is_destroying(),
					Error::<T>::NotKickerOrDestroying
				)
			},
			(false, true) => {
				// Permissionless depositor unbond is only allowed for a full unbond, and only when
				// destroying with the depositor as sole remaining member. `is_full_unbond` is
				// already guaranteed by the outer `ensure!` above.
				debug_assert!(is_full_unbond);
				ensure!(
					self.is_destroying_and_only_depositor(target_member.active_points()),
					Error::<T>::DoesNotHavePermission
				);
			},
		};

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2118-2161)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::join())]
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
			bonded_pool.ok_to_join()?;

			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: reward pool records must be updated with the old points.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;

			bonded_pool.try_inc_members()?;
			let points_issued = bonded_pool.try_bond_funds(&who, amount, BondType::Extra)?;

			PoolMembers::insert(
				who.clone(),
				PoolMember::<T> {
					pool_id,
					points: points_issued,
					// we just updated `last_known_reward_counter` to the current one in
					// `update_recorded`.
					last_recorded_reward_counter: reward_pool.last_recorded_reward_counter(),
					unbonding_eras: Default::default(),
				},
			);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2257-2337)
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

			// Note that we lazily create the unbonding pools here if they don't already exist
			let mut sub_pools = SubPoolsStorage::<T>::get(member.pool_id)
				.unwrap_or_default()
				.maybe_merge_pools(active_era);

			// Update the unbond pool associated with the current era with the unbonded funds. Note
			// that we lazily create the unbond pool if it does not yet exist.
			if !sub_pools.with_era.contains_key(&unbond_era) {
				sub_pools
					.with_era
					.try_insert(unbond_era, UnbondPool::default())
					// The above call to `maybe_merge_pools` should ensure there is
					// always enough space to insert.
					.defensive_map_err::<Error<T>, _>(|_| {
						DefensiveError::NotEnoughSpaceInUnbondPool.into()
					})?;
			}

			let points_unbonded = sub_pools
				.with_era
				.get_mut(&unbond_era)
				// The above check ensures the pool exists.
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?
				.issue(unbonding_balance);

			// Try and unbond in the member map.
			member.try_unbond(unbonding_points, points_unbonded, unbond_era)?;

			Self::deposit_event(Event::<T>::Unbonded {
				member: member_account.clone(),
				pool_id: member.pool_id,
				points: points_unbonded,
				balance: unbonding_balance,
				era: unbond_era,
			});

			// Now that we know everything has worked write the items to storage.
			SubPoolsStorage::insert(member.pool_id, sub_pools);
			Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);
			Ok(())
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2369-2400)
```rust
		/// Withdraw unbonded funds from `member_account`. If no bonded funds can be unbonded, an
		/// error is returned.
		///
		/// Under certain conditions, this call can be dispatched permissionlessly (i.e. by any
		/// account).
		///
		/// # Conditions for a permissionless dispatch
		///
		/// * The pool is in destroy mode and the target is not the depositor.
		/// * The target is the depositor and they are the only member in the sub pools.
		/// * The pool is blocked and the caller is either the root or bouncer.
		///
		/// # Conditions for permissioned dispatch
		///
		/// * The caller is the target and they are not the depositor.
		///
		/// # Note
		///
		/// - If the target is the depositor, the pool will be destroyed.
		/// - If the pool has any pending slash, we also try to slash the member before letting them
		/// withdraw. This calculation adds some weight overhead and is only defensive. In reality,
		/// pool slashes must have been already applied via permissionless [`Call::apply_slash`].
		#[pallet::call_index(5)]
		#[pallet::weight(
			T::WeightInfo::withdraw_unbonded_kill(*num_slashing_spans)
		)]
		pub fn withdraw_unbonded(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			num_slashing_spans: u32,
		) -> DispatchResultWithPostInfo {
			let caller = ensure_signed(origin)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2514-2550)
```rust
			let post_info_weight = if member.total_points().is_zero() {
				// remove any `ClaimPermission` associated with the member.
				ClaimPermissions::<T>::remove(&member_account);

				// member being reaped.
				PoolMembers::<T>::remove(&member_account);

				// Ensure any dangling delegation is withdrawn.
				let dangling_withdrawal = match T::StakeAdapter::member_delegation_balance(
					Member::from(member_account.clone()),
				) {
					Some(dangling_delegation) => {
						T::StakeAdapter::member_withdraw(
							Member::from(member_account.clone()),
							Pool::from(bonded_pool.bonded_account()),
							dangling_delegation,
							num_slashing_spans,
						)?;
						dangling_delegation
					},
					None => Zero::zero(),
				};

				Self::deposit_event(Event::<T>::MemberRemoved {
					pool_id: member.pool_id,
					member: member_account.clone(),
					released_balance: dangling_withdrawal,
				});

				if member_account == bonded_pool.roles.depositor {
					Pallet::<T>::dissolve_pool(bonded_pool);
					Weight::default()
				} else {
					bonded_pool.dec_members().put();
					SubPoolsStorage::<T>::insert(member.pool_id, sub_pools);
					T::WeightInfo::withdraw_unbonded_update(num_slashing_spans)
				}
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L979-1036)
```rust
	#[test]
	fn join_max_member_limits_are_respected() {
		ExtBuilder::default().build_and_execute(|| {
			// Given
			assert_eq!(MaxPoolMembersPerPool::<Runtime>::get(), Some(3));
			for i in 1..3 {
				let account = i + 100;
				Currency::set_balance(&account, 100 + Currency::minimum_balance());

				assert_ok!(Pools::join(RuntimeOrigin::signed(account), 100, 1));
			}

			Currency::set_balance(&103, 100 + Currency::minimum_balance());

			// Then
			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 10, pool_id: 1 },
					Event::Bonded { member: 10, pool_id: 1, bonded: 10, joined: true },
					Event::MetadataUpdated { pool_id: 1, caller: 900 },
					Event::Bonded { member: 101, pool_id: 1, bonded: 100, joined: true },
					Event::Bonded { member: 102, pool_id: 1, bonded: 100, joined: true }
				]
			);

			assert_noop!(
				Pools::join(RuntimeOrigin::signed(103), 100, 1),
				Error::<Runtime>::MaxPoolMembers
			);

			// Given
			assert_eq!(PoolMembers::<Runtime>::count(), 3);
			assert_eq!(MaxPoolMembers::<Runtime>::get(), Some(4));

			Currency::set_balance(&104, 100 + Currency::minimum_balance());
			assert_ok!(Pools::create(RuntimeOrigin::signed(104), 100, 104, 104, 104));

			let pool_account = BondedPools::<Runtime>::iter()
				.find(|(_, bonded_pool)| bonded_pool.roles.depositor == 104)
				.map(|(pool_account, _)| pool_account)
				.unwrap();

			// Then
			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 104, pool_id: 2 },
					Event::Bonded { member: 104, pool_id: 2, bonded: 100, joined: true }
				]
			);

			assert_noop!(
				Pools::join(RuntimeOrigin::signed(103), 100, pool_account),
				Error::<Runtime>::MaxPoolMembers
			);
		});
	}
```
