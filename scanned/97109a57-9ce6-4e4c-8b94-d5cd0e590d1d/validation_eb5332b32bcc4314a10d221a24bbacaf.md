### Title
`join`/`bond_extra` in `pallet-nomination-pools` mint new points at a stale (pre-slash) exchange rate for members with an unapplied pending slash - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`LienToken.createLien` created a new loan without checking whether the borrower's existing debt should already have triggered liquidation, because it relied on someone having permissionlessly called `AstariaRouter.liquidate` first. If nobody called it, the check silently passed and a new loan was granted on top of unresolved debt. The same "unsettled obligation + permissionless settlement + no pre-check in the state-changing entrypoint" pattern exists in `pallet-nomination-pools`: `join`/`bond_extra` mint new pool points using the pool's current `points`/`balance()` ratio without first requiring `do_apply_slash` to be applied for the acting member or the pool, even though a pending slash (`T::StakeAdapter::pending_slash`) may already exist and is only resolved by the separate, permissionless `apply_slash` extrinsic.

### Finding Description
`Call::join` [1](#0-0)  and `do_bond_extra` [2](#0-1)  both call `bonded_pool.try_bond_funds(...)`, which issues points via `self.issue(amount)` computed from the pool's currently recorded `points`/`balance()` before actually transferring/bonding the funds [3](#0-2) .

Unlike `withdraw_unbonded`, which explicitly settles the member's outstanding obligation first (`Self::do_apply_slash(&member_account, None, false)` before computing `balance_to_unbond`) [4](#0-3) , neither `join` nor `do_bond_extra` calls `do_apply_slash` before minting new points. `do_apply_slash`/`member_pending_slash` exist specifically to reconcile a member's recorded balance against the actual (already-slashed) delegated stake [5](#0-4) [6](#0-5) , but that settlement is itself gated behind a separate permissionless `apply_slash` call — exactly like Astaria's liquidation, which required someone to call `AstariaRouter.liquidate` before the "is this borrower safe" check would reflect reality.

The doc comment on `withdraw_unbonded` even states this is treated only as a defensive backstop ("In reality, pool slashes must have been already applied via permissionless `Call::apply_slash`") [7](#0-6) , confirming that the invariant "pending slash is settled before further pool state changes" is expected but not uniformly enforced across every mutating entrypoint — `join`/`bond_extra` are the entrypoints where it is missing.

### Impact Explanation
When a pool (or a specific member's underlying delegation) has an unresolved slash, `PoolMembers`/`BondedPool.points` still reflect the pre-slash accounting while the actual staked/delegated balance behind the pool is already reduced. A `join` or `bond_extra` call in this window issues points at the stale, more favorable points-per-balance ratio, letting the new/topping-up member acquire a disproportionately large claim on the pool's value relative to depositors who already absorbed (or will absorb) the slash. This under/over-values funds mid-flight, socializing the pending loss onto other/earlier members and diluting their entitlement once `apply_slash` eventually runs — a value-conservation violation directly analogous to granting a new loan while a borrower's existing default has not yet been recognized.

### Likelihood Explanation
Triggering the window requires only that: (1) an offence/slash has been reported and applied to the staking ledger (`pallet_staking(_async)::slashing::do_slash`) but (2) nobody has yet called the permissionless `apply_slash` for the pool/member. This is a normal, expected delay in production (slash application and pool-level reconciliation happen asynchronously and are advertised as being handled "eventually" by anyone), so an attacker (a new joiner or an existing member wanting to bond_extra) does not need any privileged role — they only need to act during the gap before `apply_slash` is called, which they can even influence by choosing not to call it themselves.

### Recommendation
Require `Self::do_apply_slash` (or an equivalent pending-slash check with `enforce_min_slash = false`, ignoring `NothingToSlash`) to run for the pool/member before `join` and `do_bond_extra` compute `try_bond_funds`/issue points, mirroring the pattern already used in `withdraw_unbonded`. Alternatively, recompute the points/balance exchange rate immediately after settling any pending slash, so new points are always issued against the true, post-slash pool valuation.

### Proof of Concept
1. Create pool `P` with depositor `D`, bonded balance `B0`, `points = B0`.
2. Advance era; a slashing event reduces the actual staked/delegated balance backing `P` (e.g., via `pallet_staking(_async)::slashing::do_slash`), producing `pending_slash > 0` (as validated in tests such as `pool_partially_slashed` / `join_works`, which show `PoolSlashed` only fires once someone calls into pool code without a prior explicit `apply_slash`) [8](#0-7) .
3. Before anyone calls `Call::apply_slash`, a new member `M` calls `Call::join(amount, P)` (or an existing member calls `bond_extra`). `try_bond_funds` issues points using the pool's currently recorded `points`/`balance()` ratio, which has not yet been corrected for the slash [9](#0-8) .
4. When `apply_slash` is later called for the still-unsettled member(s), the slash is applied against the pool's now-diluted point base, so the loss lands disproportionately on the original/other members rather than being reflected in `M`'s newly issued points — demonstrating the mispriced entry analogous to a new "loan" issued while the prior obligation (slash/debt) had not been settled.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1308-1337)
```rust
	/// Bond exactly `amount` from `who`'s funds into this pool. Increases the [`TotalValueLocked`]
	/// by `amount`.
	///
	/// If the bond is [`BondType::Create`], [`Staking::bond`] is called, and `who` is allowed to be
	/// killed. Otherwise, [`Staking::bond_extra`] is called and `who` cannot be killed.
	///
	/// Returns `Ok(points_issues)`, `Err` otherwise.
	fn try_bond_funds(
		&mut self,
		who: &T::AccountId,
		amount: BalanceOf<T>,
		ty: BondType,
	) -> Result<BalanceOf<T>, DispatchError> {
		// We must calculate the points issued *before* we bond who's funds, else points:balance
		// ratio will be wrong.
		let points_issued = self.issue(amount);

		T::StakeAdapter::pledge_bond(
			Member::from(who.clone()),
			Pool::from(self.bonded_account()),
			&self.reward_account(),
			amount,
			ty,
		)?;
		TotalValueLocked::<T>::mutate(|tvl| {
			tvl.saturating_accrue(amount);
		});

		Ok(points_issued)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2118-2174)
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

			Self::deposit_event(Event::<T>::Bonded {
				member: who,
				pool_id,
				bonded: amount,
				joined: true,
			});

			bonded_pool.put();
			RewardPools::<T>::insert(pool_id, reward_pool);

			Ok(())
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2385-2390)
```rust
		/// # Note
		///
		/// - If the target is the depositor, the pool will be destroyed.
		/// - If the pool has any pending slash, we also try to slash the member before letting them
		/// withdraw. This calculation adds some weight overhead and is only defensive. In reality,
		/// pool slashes must have been already applied via permissionless [`Call::apply_slash`].
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2417-2432)
```rust
			let slash_weight =
				// apply slash if any before withdraw.
				match Self::do_apply_slash(&member_account, None, false) {
					Ok(_) => T::WeightInfo::apply_slash(),
					Err(e) => {
						let no_pending_slash: DispatchResult = Err(Error::<T>::NothingToSlash.into());
						// This is an expected error. We add appropriate fees and continue withdrawal.
						if Err(e) == no_pending_slash {
							T::WeightInfo::apply_slash_fail()
						} else {
							// defensive: if we can't apply slash for some reason, we abort.
							return Err(Error::<T>::Defensive(DefensiveError::SlashNotApplied).into());
						}
					}

				};
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3652-3707)
```rust
	fn do_bond_extra(
		signer: T::AccountId,
		member_account: T::AccountId,
		extra: BondExtra<BalanceOf<T>>,
	) -> DispatchResult {
		// ensure account is not restricted from joining the pool.
		ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted);

		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_bond_extra(),
				Error::<T>::DoesNotHavePermission
			);
			ensure!(extra == BondExtra::Rewards, Error::<T>::BondExtraRestricted);
		}

		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		// payout related stuff: we must claim the payouts, and updated recorded payout data
		// before updating the bonded pool points, similar to that of `join` transaction.
		reward_pool.update_records(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
		let claimed = Self::do_reward_payout(
			&member_account,
			&mut member,
			&mut bonded_pool,
			&mut reward_pool,
		)?;

		let (points_issued, bonded) = match extra {
			BondExtra::FreeBalance(amount) => {
				(bonded_pool.try_bond_funds(&member_account, amount, BondType::Extra)?, amount)
			},
			BondExtra::Rewards => {
				(bonded_pool.try_bond_funds(&member_account, claimed, BondType::Extra)?, claimed)
			},
		};

		bonded_pool.ok_to_be_open()?;
		member.points =
			member.points.checked_add(&points_issued).ok_or(Error::<T>::OverflowRisk)?;

		Self::deposit_event(Event::<T>::Bonded {
			member: member_account.clone(),
			pool_id: member.pool_id,
			bonded,
			joined: false,
		});
		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3816-3841)
```rust
	/// Slash member against the pending slash for the pool.
	fn do_apply_slash(
		member_account: &T::AccountId,
		reporter: Option<T::AccountId>,
		enforce_min_slash: bool,
	) -> DispatchResult {
		let member = PoolMembers::<T>::get(member_account).ok_or(Error::<T>::PoolMemberNotFound)?;

		let pending_slash =
			Self::member_pending_slash(Member::from(member_account.clone()), member.clone())?;

		// ensure there is something to slash.
		ensure!(!pending_slash.is_zero(), Error::<T>::NothingToSlash);

		if enforce_min_slash {
			// ensure slashed amount is at least the minimum balance.
			ensure!(pending_slash >= T::Currency::minimum_balance(), Error::<T>::SlashTooLow);
		}

		T::StakeAdapter::member_slash(
			Member::from(member_account.clone()),
			Pool::from(Pallet::<T>::generate_bonded_account(member.pool_id)),
			pending_slash,
			reporter,
		)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3843-3873)
```rust
	/// Pending slash for a member.
	///
	/// Takes the pool_member object corresponding to the `member_account`.
	fn member_pending_slash(
		member_account: Member<T::AccountId>,
		pool_member: PoolMember<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		// only executed in tests: ensure the member account is correct.
		debug_assert!(
			PoolMembers::<T>::get(member_account.clone().get()).expect("member must exist") ==
				pool_member
		);

		let pool_account = Pallet::<T>::generate_bonded_account(pool_member.pool_id);
		// if the pool doesn't have any pending slash, it implies the member also does not have any
		// pending slash.
		if T::StakeAdapter::pending_slash(Pool::from(pool_account.clone())).is_zero() {
			return Ok(Zero::zero());
		}

		// this is their actual held balance that may or may not have been slashed.
		let actual_balance = T::StakeAdapter::member_delegation_balance(member_account)
			// no delegation implies the member delegation is not migrated yet to `DelegateStake`.
			.ok_or(Error::<T>::NotMigrated)?;

		// this is their balance in the pool
		let expected_balance = pool_member.total_balance();

		// return the amount to be slashed.
		Ok(actual_balance.saturating_sub(expected_balance))
	}
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L812-880)
```rust
mod join {
	use super::*;

	#[test]
	fn join_works() {
		let bonded = |points, member_counter| BondedPool::<Runtime> {
			id: 1,
			inner: BondedPoolInner {
				commission: Commission::default(),
				member_counter,
				points,
				roles: DEFAULT_ROLES,
				state: PoolState::Open,
			},
		};
		ExtBuilder::default().with_check(0).build_and_execute(|| {
			// Given
			Currency::set_balance(&11, ExistentialDeposit::get() + 2);
			assert!(!PoolMembers::<Runtime>::contains_key(11));
			assert_eq!(TotalValueLocked::<T>::get(), 10);

			// When
			assert_ok!(Pools::join(RuntimeOrigin::signed(11), 2, 1));

			// Then
			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 10, pool_id: 1 },
					Event::Bonded { member: 10, pool_id: 1, bonded: 10, joined: true },
					Event::MetadataUpdated { pool_id: 1, caller: 900 },
					Event::Bonded { member: 11, pool_id: 1, bonded: 2, joined: true },
				]
			);
			assert_eq!(TotalValueLocked::<T>::get(), 12);

			assert_eq!(
				PoolMembers::<Runtime>::get(11).unwrap(),
				PoolMember::<Runtime> { pool_id: 1, points: 2, ..Default::default() }
			);
			assert_eq!(BondedPool::<Runtime>::get(1).unwrap(), bonded(12, 2));

			// Given
			// The bonded balance is slashed in half
			StakingMock::slash_by(1, 6);

			// And
			Currency::set_balance(&12, ExistentialDeposit::get() + 12);
			assert!(!PoolMembers::<Runtime>::contains_key(12));

			// When
			assert_ok!(Pools::join(RuntimeOrigin::signed(12), 12, 1));

			// Then
			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::PoolSlashed { pool_id: 1, balance: 6 },
					Event::Bonded { member: 12, pool_id: 1, bonded: 12, joined: true }
				]
			);
			assert_eq!(TotalValueLocked::<T>::get(), 18);

			assert_eq!(
				PoolMembers::<Runtime>::get(12).unwrap(),
				PoolMember::<Runtime> { pool_id: 1, points: 24, ..Default::default() }
			);
			assert_eq!(BondedPool::<Runtime>::get(1).unwrap(), bonded(12 + 24, 3));
		});
```
