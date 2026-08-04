### Title
Pool-slot Denial of Service via dust joins that occupy `MaxPoolMembersPerPool` slots for the full unbonding duration - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools` enforces a per-pool member cap (`MaxPoolMembersPerPool`) checked in `BondedPool::ok_to_be_open`/`try_inc_members` during `join`, exactly analogous to the audited `delegation_service`'s `MAX_NUMBER_OF_DELEGATIONS = 100`. The only value gate at join time is `MinJoinBond`, which is admin-configured and defaults to `Zero::zero()` in `GenesisConfig::default()`. An attacker can fill a target pool's member slots with many sybil accounts joining with dust (or zero, if `MinJoinBond` is 0/low), then immediately call `unbond` on each — the member is not removed from `PoolMembers`/`member_counter` at that point, only placed into an unbonding state. The slot therefore stays occupied for the entire bonding duration, permanently locking out legitimate stakers from an open pool for a real-world period (e.g. ~28 eras / multiple days on Polkadot/Kusama-class configurations), at negligible cost to the attacker.

### Finding Description
`join` enforces the member cap: [1](#0-0) 

The only economic gate is:
```
ensure!(amount >= MinJoinBond::<T>::get(), Error::<T>::MinimumBondNotMet);
```
`MinJoinBond` is a mutable `StorageValue` whose default (`GenesisConfig::default()`) is zero: [2](#0-1) 

Unlike the pool-level cap on member count, there is no lifetime/minimum-stake enforcement that prevents an account from joining with the smallest allowed amount and then unbonding immediately. `unbond` only checks the balance-after-unbond condition when the unbond is *partial*; a *full* unbond bypasses the `MinJoinBond` floor entirely: [3](#0-2) 

Crucially, calling `unbond` does **not** remove the member from `PoolMembers` or decrement `BondedPoolInner::member_counter` — it only moves the member's points into `unbonding_eras`. The member (and hence the occupied slot counted against `MaxPoolMembersPerPool`) is only actually removed once `withdraw_unbonded` succeeds after the bonding duration elapses, which is confirmed by pool test expectations showing `member_counter` unchanged immediately after `fully_unbond`, and only decremented (via `MemberRemoved`) at final withdrawal: [4](#0-3) [5](#0-4) 

The pool member cap is checked and demonstrated in the existing test suite, confirming that once `MaxPoolMembersPerPool`/`MaxPoolMembers` is reached, further `join` calls hard-fail with `Error::MaxPoolMembers`: [6](#0-5) 

### Impact Explanation
This is a direct structural analog of the reported `delegation_service` issue: a public, unprivileged, permissionless entry point (`join`) lets an attacker occupy all of a bounded resource's slots with fake/dust participants, denying legitimate users the ability to stake into that pool. Because `unbond` does not free the slot immediately, the attacker can hold every slot in a target pool hostage for the full unbonding/bonding duration using minimal capital (bounded only by `MinJoinBond`, which can be zero or very low, and by the existential deposit). This degrades the intended behavior of nomination pools (a core staking primitive) and can be repeated against any open pool an attacker wants to block, without needing governance, a malicious validator, or any privileged role — matching the "runtime bug that compromises intended behavior" / "public underpriced work" impact classes.

### Likelihood Explanation
Likelihood is moderate-to-high wherever `MinJoinBond` is left at or near the default zero value, or set low relative to `MaxPoolMembersPerPool`/`MaxPoolMembers`; the attacker only needs to control `MaxPoolMembersPerPool` accounts each funded with the existential deposit plus `MinJoinBond`, well within reach of a single actor since no other permission or validator collusion is required. The exact cost/likelihood depends on runtime-specific configuration of `MinJoinBond` and `MaxPoolMembersPerPool`, which is set by governance per-chain — this is the main source of uncertainty, since a sufficiently high `MinJoinBond` (set via the `set_configs` admin call) would raise the attack cost, though the protocol itself does not prevent the zero/low-cost configuration from being live.

### Recommendation
- Enforce `MinJoinBond` (or a similar minimum) as a persistent condition on the member's *remaining* bonded balance while occupying a pool slot, not just at the moment of `join`, so dust members cannot linger while unbonding.
- Consider decrementing `member_counter` (freeing the slot) as soon as a member enters a fully-unbonding state rather than waiting for `withdraw_unbonded`, or introduce a separate "reserved-for-unbonding" counter distinct from the active member cap so genuine joiners are not blocked by unbonding dust accounts.
- Alternatively/additionally, charge a per-slot deposit for joining a pool (refunded on legitimate long-term participation) to make sybil slot-filling economically costly regardless of `MinJoinBond` configuration.

### Proof of Concept
1. Chain configures (or defaults to) a low/zero `MinJoinBond` and a bounded `MaxPoolMembersPerPool` (e.g., default test value 3, or a realistic on-chain value).
2. Attacker controls `MaxPoolMembersPerPool - 1` funded accounts (each funded to existential deposit + `MinJoinBond`).
3. For each sybil account, call `Pools::join(origin, MinJoinBond_amount, target_pool_id)` — succeeds because `amount >= MinJoinBond` (line 2132) and slots are still available.
4. Once the pool is full, a legitimate user's `Pools::join` call fails with `Error::MaxPoolMembers` (as demonstrated in `join_max_member_limits_are_respected`, lines 1005-1008).
5. Attacker immediately calls `Pools::unbond(origin, sybil_account, full_points)` for each sybil account — succeeds as a full unbond (bypasses the `MinimumBondNotMet` balance check per lines 1243-1252) but does **not** remove the `PoolMembers` entry or decrement `member_counter` (confirmed by `unbond_kick_works`/`withdraw_unbonded_kick` tests showing `member_counter` unchanged post-unbond).
6. The pool remains full (blocking legitimate joiners) until the bonding duration elapses and `withdraw_unbonded` is called for each sybil account, at which point the attacker can immediately re-join with the same dust amounts and repeat, sustaining the DoS indefinitely at minimal capital cost.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1236-1252)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1842-1852)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2130-2149)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L979-1008)
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
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L3264-3277)
```rust
				assert_eq!(
					BondedPool::<Runtime>::get(1).unwrap(),
					BondedPool {
						id: 1,
						inner: BondedPoolInner {
							commission: Commission::default(),
							member_counter: 3,
							points: 10, // Only 10 points because 200 + 100 was unbonded
							roles: DEFAULT_ROLES,
							state: PoolState::Blocked,
						}
					}
				);
				assert_eq!(StakingMock::active_stake(&default_bonded_account()).unwrap(), 10);
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L4113-4133)
```rust
	#[test]
	fn withdraw_unbonded_kick() {
		ExtBuilder::default()
			.add_members(vec![(100, 100), (200, 200)])
			.build_and_execute(|| {
				// Given
				assert_ok!(Pools::fully_unbond(RuntimeOrigin::signed(100), 100));
				assert_ok!(Pools::fully_unbond(RuntimeOrigin::signed(200), 200));
				assert_eq!(
					BondedPool::<Runtime>::get(1).unwrap(),
					BondedPool {
						id: 1,
						inner: BondedPoolInner {
							commission: Commission::default(),
							member_counter: 3,
							points: 10,
							roles: DEFAULT_ROLES,
							state: PoolState::Open,
						}
					}
				);
```
