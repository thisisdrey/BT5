### Title
Nomination pool commission has no protocol-enforced ceiling — pool operators can set commission to 100% and expropriate member rewards - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`pallet-nomination-pools` lets the permissionless creator/`root` of *any* pool set that pool's reward commission via `set_commission`. The only ceiling is `GlobalMaxCommission`, a chain-level `StorageValue<_, Perbill, OptionQuery>` that must be explicitly set by governance. When it is `None` (its genesis default), `try_update_current`/`current()` fall back to `Bounded::max_value()`, i.e. **100%**, so an ordinary, unprivileged pool operator can legally set commission to 100% and take every unit of reward that would otherwise go to bonded members — the direct analog of the Native `widgetFee` allowing market makers to charge up to 100% of the swapper's input.

### Finding Description
`Commission::current()` and `Commission::try_update_current()` bound the commission only against `GlobalMaxCommission::<T>::get().unwrap_or(Bounded::max_value())`: [1](#0-0) 

`set_commission` (call index 17) is reachable by any signed account holding the pool's `root`/commission-manager role — which is simply whoever created the pool or was assigned that role, not a chain-privileged actor: [2](#0-1) 

`GlobalMaxCommission` defaults to `None` in the pallet's `GenesisConfig::default()`, and the reference node's default genesis JSON ships with `"globalMaxCommission": null`: [3](#0-2) 

The behavior is exercised directly by the pallet's own tests, which confirm that with no global cap set, a pool operator can set commission to 100% and it is honored on payout, giving the commission payee the entire reward and zero to the member: [4](#0-3) [5](#0-4) 

Only when a runtime's governance proactively calls `set_configs` (root-only) to populate `GlobalMaxCommission` is any ceiling enforced at all: [6](#0-5) 

This mirrors the Native report precisely: a permissionless, non-privileged party (pool creator/root, analogous to the "market maker") is allowed by protocol design to set a fee-like rate (commission) all the way to 100% of the counterparty's (member's) proceeds, because the pallet's own default ceiling is effectively unbounded rather than a sane maximum (e.g. the report's suggested 10–30%).

### Impact Explanation
Any account can permissionlessly create a nomination pool (`create`/`create_with_pool_id`), advertise a low or zero commission to attract joiners, then raise it toward 100% (subject only to any self-imposed `max`/`change_rate` they themselves configured — which they fully control and can set as permissively as they like). Members who bonded funds and are earning staking rewards through the pool can have their entire reward share diverted to the operator's chosen payee, with only the bonded principal remaining theirs. This is a direct, protocol-level fund-diversion/value-conservation violation for reward payouts (not a governance/admin abuse — the pool operator role is an ordinary permissionless actor, identical in class to the "market maker" in the report), and it materializes by default unless the runtime's governance takes an affirmative extra step to populate `GlobalMaxCommission`.

### Likelihood Explanation
High: no privileged action, malicious validator, relayer, or leaked key is needed — only creating a pool (or already being a pool's `root`) and calling the public `set_commission`/`set_commission_max` extrinsics, which succeed whenever no `GlobalMaxCommission` has been configured (the shipped default). The change-rate throttling mechanism is opt-in and controlled by the same operator, so it provides no protection against an operator who never sets it or sets it to allow instant jumps to 100%.

### Recommendation
Do not leave `GlobalMaxCommission` unbounded by default. Either:
- Enforce a hard-coded, pallet-level sane maximum (e.g. in the 10–50% range) independent of `GlobalMaxCommission`, so a chain that never configures the global cap is still protected, or
- Change the genesis default to a non-`None`, conservative value, and require all pool commissions to be bounded by that value from pool creation rather than only from the moment governance calls `set_configs`.

### Proof of Concept
Using the pallet's own test harness (`substrate/frame/nomination-pools/src/tests.rs`), with `GlobalMaxCommission::<Runtime>::set(None)`:
1. Pool 1 exists with depositor 10 bonded.
2. `Pools::set_commission(RuntimeOrigin::signed(900), 1, Some((Perbill::from_percent(100), 2)))` succeeds (no error), setting commission to 100% with payee `2`.
3. `deposit_rewards(10)` credits 10 points of reward to the pool.
4. `Pools::do_reward_payout(&10, &mut member, &mut bonded_pool, &mut reward_pool)` executes without error but member `10` receives `0` payout — all 10 reward points go to commission payee `2`.

This exact sequence is already codified in the repository as `do_reward_payout_with_100_percent_commission`: [4](#0-3) 

demonstrating that the only thing preventing 100% commission (and total member reward expropriation) on a live chain is whether that chain's governance has separately configured `GlobalMaxCommission` — which is not enforced by the pallet's defaults or by any hard ceiling in `try_update_current`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L843-879)
```rust
	/// Gets the pool's current commission, or returns Perbill::zero if none is set.
	/// Bounded to global max if current is greater than `GlobalMaxCommission`.
	fn current(&self) -> Perbill {
		self.current
			.as_ref()
			.map_or(Perbill::zero(), |(c, _)| *c)
			.min(GlobalMaxCommission::<T>::get().unwrap_or(Bounded::max_value()))
	}

	/// Set the pool's commission.
	///
	/// Update commission based on `current`. If a `None` is supplied, allow the commission to be
	/// removed without any change rate restrictions. Updates `throttle_from` to the current block.
	/// If the supplied commission is zero, `None` will be inserted and `payee` will be ignored.
	fn try_update_current(&mut self, current: &Option<(Perbill, T::AccountId)>) -> DispatchResult {
		self.current = match current {
			None => None,
			Some((commission, payee)) => {
				ensure!(!self.throttling(commission), Error::<T>::CommissionChangeThrottled);
				ensure!(
					commission <= &GlobalMaxCommission::<T>::get().unwrap_or(Bounded::max_value()),
					Error::<T>::CommissionExceedsGlobalMaximum
				);
				ensure!(
					self.max.map_or(true, |m| commission <= &m),
					Error::<T>::CommissionExceedsMaximum
				);
				if commission.is_zero() {
					None
				} else {
					Some((*commission, payee.clone()))
				}
			},
		};
		self.register_update();
		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1832-1853)
```rust
	#[pallet::genesis_config]
	pub struct GenesisConfig<T: Config> {
		pub min_join_bond: BalanceOf<T>,
		pub min_create_bond: BalanceOf<T>,
		pub max_pools: Option<u32>,
		pub max_members_per_pool: Option<u32>,
		pub max_members: Option<u32>,
		pub global_max_commission: Option<Perbill>,
	}

	impl<T: Config> Default for GenesisConfig<T> {
		fn default() -> Self {
			Self {
				min_join_bond: Zero::zero(),
				min_create_bond: Zero::zero(),
				max_pools: Some(16),
				max_members_per_pool: Some(32),
				max_members: Some(16 * 32),
				global_max_commission: None,
			}
		}
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2738-2778)
```rust
		#[pallet::call_index(11)]
		#[pallet::weight(T::WeightInfo::set_configs())]
		pub fn set_configs(
			origin: OriginFor<T>,
			min_join_bond: ConfigOp<BalanceOf<T>>,
			min_create_bond: ConfigOp<BalanceOf<T>>,
			max_pools: ConfigOp<u32>,
			max_members: ConfigOp<u32>,
			max_members_per_pool: ConfigOp<u32>,
			global_max_commission: ConfigOp<Perbill>,
		) -> DispatchResult {
			T::AdminOrigin::ensure_origin(origin)?;

			macro_rules! config_op_exp {
				($storage:ty, $op:ident) => {
					match $op {
						ConfigOp::Noop => (),
						ConfigOp::Set(v) => <$storage>::put(v),
						ConfigOp::Remove => <$storage>::kill(),
					}
				};
			}

			config_op_exp!(MinJoinBond::<T>, min_join_bond);
			config_op_exp!(MinCreateBond::<T>, min_create_bond);
			config_op_exp!(MaxPools::<T>, max_pools);
			config_op_exp!(MaxPoolMembers::<T>, max_members);
			config_op_exp!(MaxPoolMembersPerPool::<T>, max_members_per_pool);
			config_op_exp!(GlobalMaxCommission::<T>, global_max_commission);

			Self::deposit_event(Event::<T>::GlobalParamsUpdated {
				min_join_bond: MinJoinBond::<T>::get(),
				min_create_bond: MinCreateBond::<T>::get(),
				max_pools: MaxPools::<T>::get(),
				max_members: MaxPoolMembers::<T>::get(),
				max_members_per_pool: MaxPoolMembersPerPool::<T>::get(),
				global_max_commission: GlobalMaxCommission::<T>::get(),
			});

			Ok(())
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2963-2995)
```rust
		#[pallet::call_index(17)]
		#[pallet::weight(T::WeightInfo::set_commission())]
		pub fn set_commission(
			origin: OriginFor<T>,
			pool_id: PoolId,
			new_commission: Option<(Perbill, T::AccountId)>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let mut bonded_pool = BondedPool::<T>::get(pool_id).ok_or(Error::<T>::PoolNotFound)?;
			// ensure pool is not in an un-migrated state.
			ensure!(!Self::api_pool_needs_delegate_migration(pool_id), Error::<T>::NotMigrated);

			ensure!(bonded_pool.can_manage_commission(&who), Error::<T>::DoesNotHavePermission);

			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: make sure that everything up to this point is using the current commission
			// before it updates. Note that `try_update_current` could still fail at this point.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);

			bonded_pool.commission.try_update_current(&new_commission)?;
			bonded_pool.put();
			Self::deposit_event(Event::<T>::PoolCommissionUpdated {
				pool_id,
				current: new_commission,
			});
			Ok(())
		}
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L7139-7231)
```rust
	#[test]
	fn do_reward_payout_with_various_commissions() {
		ExtBuilder::default().build_and_execute(|| {
			// turn off GlobalMaxCommission for this test.
			GlobalMaxCommission::<Runtime>::set(None);
			let pool_id = 1;

			// top up commission payee account to existential deposit
			let _ = Currency::set_balance(&2, 5);

			// Set a commission pool 1 to 33%, with a payee set to `2`
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(900),
				pool_id,
				Some((Perbill::from_percent(33), 2)),
			));
			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 10, pool_id: 1 },
					Event::Bonded { member: 10, pool_id: 1, bonded: 10, joined: true },
					Event::MetadataUpdated { pool_id: 1, caller: 900 },
					Event::PoolCommissionUpdated {
						pool_id: 1,
						current: Some((Perbill::from_percent(33), 2))
					},
				]
			);

			// The pool earns 10 points
			deposit_rewards(10);
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));

			// Then:
			assert_eq!(
				pool_events_since_last_call(),
				vec![Event::PaidOut { member: 10, pool_id: 1, payout: 7 },]
			);

			// The pool earns 17 points
			deposit_rewards(17);
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));

			// Then:
			assert_eq!(
				pool_events_since_last_call(),
				vec![Event::PaidOut { member: 10, pool_id: 1, payout: 11 },]
			);

			// The pool earns 50 points
			deposit_rewards(50);
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));

			// Then:
			assert_eq!(
				pool_events_since_last_call(),
				vec![Event::PaidOut { member: 10, pool_id: 1, payout: 34 },]
			);

			// The pool earns 10439 points
			deposit_rewards(10439);
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));

			// Then:
			assert_eq!(
				pool_events_since_last_call(),
				vec![Event::PaidOut { member: 10, pool_id: 1, payout: 6994 },]
			);

			// Set the commission to 100% and ensure the following payout to the pool member will
			// not happen.

			// When:
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(900),
				pool_id,
				Some((Perbill::from_percent(100), 2)),
			));

			// Given:
			deposit_rewards(200);
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));

			// Then:
			assert_eq!(
				pool_events_since_last_call(),
				vec![Event::PoolCommissionUpdated {
					pool_id: 1,
					current: Some((Perbill::from_percent(100), 2))
				},]
			);
		})
	}
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L7354-7397)
```rust
	#[test]
	fn do_reward_payout_with_100_percent_commission() {
		ExtBuilder::default().build_and_execute(|| {
			// turn off GlobalMaxCommission for this test.
			GlobalMaxCommission::<Runtime>::set(None);

			let (mut member, bonded_pool, mut reward_pool) =
				Pools::get_member_with_pools(&10).unwrap();

			// top up commission payee account to existential deposit
			let _ = Currency::set_balance(&2, 5);

			// Set a commission pool 1 to 100%, with a payee set to `2`
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(900),
				bonded_pool.id,
				Some((Perbill::from_percent(100), 2)),
			));

			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 10, pool_id: 1 },
					Event::Bonded { member: 10, pool_id: 1, bonded: 10, joined: true },
					Event::MetadataUpdated { pool_id: 1, caller: 900 },
					Event::PoolCommissionUpdated {
						pool_id: 1,
						current: Some((Perbill::from_percent(100), 2))
					}
				]
			);

			// The pool earns 10 points
			deposit_rewards(10);

			// execute the payout
			assert_ok!(Pools::do_reward_payout(
				&10,
				&mut member,
				&mut BondedPool::<Runtime>::get(1).unwrap(),
				&mut reward_pool
			));
		})
	}
```
