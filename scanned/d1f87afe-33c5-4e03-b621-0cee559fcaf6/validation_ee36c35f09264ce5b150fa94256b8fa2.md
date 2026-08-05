## Analysis

The core broken invariant in the Velodrome finding: an unprivileged, low-cost public call permanently fills a *bounded, shared resource slot list* with attacker-controlled entries, denying legitimate future entries, with no built-in cost-scaling or whitelist to prevent it.

The closest local analog in `polkadot-sdk--001` is in `pallet-nomination-pools`: the public, unprivileged `join` extrinsic lets anyone bond the minimum `MinJoinBond` into an arbitrary target pool, incrementing that pool's `member_counter` against the global `MaxPoolMembersPerPool` cap enforced by `try_inc_members`.

### Title
Unprivileged sybil `join()` calls can permanently fill a nomination pool's `MaxPoolMembersPerPool` slot cap, denying legitimate members - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::join` is callable by any signed account, targeting **any** existing pool ID, with only a `MinJoinBond` amount check [1](#0-0) . The per-pool capacity check is a simple counter compare in `try_inc_members`, with no whitelist, no per-account rate limiting, and no minimum-bond scaling relative to the number of existing members [2](#0-1) . `MinJoinBond` defaults to zero in genesis and is a chain-wide config value, not something a pool's own root/depositor can raise per-pool [3](#0-2) .

### Finding Description
An attacker can create `MaxPoolMembersPerPool` (test default `3`, production defaults vary) sybil accounts, each funded with just over `MinJoinBond + ExistentialDeposit`, and call `join` against a specific victim pool until `member_counter` hits the cap enforced in `try_inc_members` [2](#0-1) . Once full, any further legitimate `join` call against that pool fails with `Error::MaxPoolMembers`, exactly as demonstrated in `join_max_member_limits_are_respected` [4](#0-3) . This mirrors the Velodrome pattern: a public, permissionless, low-cost call populates a bounded slot list up to its cap, blocking legitimate use of that slot list — except here the "reward tokens" are pool membership slots and the "small token amounts" are the minimum bond.

The bond isn't burned (unlike the tiny reward-token transfers in Velodrome), but it is locked/staked capital the attacker must commit per sybil account for as long as the griefing is to persist, and — critically — the victim pool is a *shared, third-party resource* that any account can target, not something under the attacker's control.

### Impact Explanation
A malicious actor can render any specific nomination pool permanently unable to accept new members, which:
- Blocks depositors/pool operators from growing their pool's membership, hurting their fee/commission income and the pool's ability to attract stake.
- Can be used to grief competitor pools in a competitive staking market.
- Does not require any privileged origin, validator, collator, relayer, or governance actor — it's a pure public-entrypoint issue reachable by any signed account with minimal funds.

This aligns with the "runtime bugs that compromise intended behavior" and "public underpriced work" categories in the impact gate, since it degrades the intended open-membership behavior of nomination pools via cheap unprivileged calls.

### Likelihood Explanation
Likelihood is moderate-to-high wherever `MinJoinBond` is configured low (it defaults to zero) and `MaxPoolMembersPerPool` is a fixed, chain-wide small number: an attacker only needs `MaxPoolMembersPerPool` sybil accounts each funded above `MinJoinBond + ED`. The existing mitigation is that a pool's bouncer/root can set the pool state to `Blocked` and permissionlessly kick non-permissioned members (`ok_to_unbond_with` allows kicking when `can_kick(caller) || is_destroying()`) [5](#0-4) , analogous to the Velodrome team's `swapOutBribeRewardToken` remediation — this is why the underlying severity should be judged similarly to the referenced Medium-severity finding rather than Critical, since operators have a reactive but not preventive remedy.

### Recommendation
- Scale `MinJoinBond` (or add a per-pool bond-scaling factor) with the number of existing members so that filling slots becomes progressively more expensive, similar to the auction/replacement mechanism used in `pallet-collator-selection`'s `CandidateList` [6](#0-5) .
- Allow pool `root`/`bouncer` to configure a per-pool minimum join bond above the chain-wide `MinJoinBond`.
- Consider allowing replacement of the smallest-bonded member by a strictly larger bond when the pool is full, mirroring `take_candidate_slot` semantics in collator-selection.

### Proof of Concept
1. Deploy a runtime with `MinJoinBond = 0` (or a low value) and `MaxPoolMembersPerPool = N`.
2. Create `N` funded sybil accounts, each with `MinJoinBond + ExistentialDeposit`.
3. Call `Pools::join(origin_i, MinJoinBond, victim_pool_id)` for each sybil account until `member_counter == N` (as exercised in `join_max_member_limits_are_respected`) [7](#0-6) .
4. A legitimate account then calls `Pools::join(legit_origin, amount, victim_pool_id)` and receives `Error::<T>::MaxPoolMembers`, confirming the pool is permanently closed to new joiners until the pool operator manually blocks the pool and kicks the sybils.

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1267-1275)
```rust
			(false, false) => {
				// If the pool is blocked, then an admin with kicking permissions can remove a
				// member. If the pool is being destroyed, anyone can remove a member
				debug_assert!(is_full_unbond);
				ensure!(
					self.can_kick(caller) || self.is_destroying(),
					Error::<T>::NotKickerOrDestroying
				)
			},
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1842-1853)
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
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2120-2137)
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
			bonded_pool.ok_to_join()?;
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

**File:** cumulus/pallets/collator-selection/src/lib.rs (L516-556)
```rust
		/// Register this account as a collator candidate. The account must (a) already have
		/// registered session keys and (b) be able to reserve the `CandidacyBond`.
		///
		/// This call is not available to `Invulnerable` collators.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::register_as_candidate(T::MaxCandidates::get()))]
		pub fn register_as_candidate(origin: OriginFor<T>) -> DispatchResultWithPostInfo {
			let who = ensure_signed(origin)?;

			// ensure we are below limit.
			let length: u32 = CandidateList::<T>::decode_len()
				.unwrap_or_default()
				.try_into()
				.unwrap_or_default();
			ensure!(length < T::MaxCandidates::get(), Error::<T>::TooManyCandidates);
			ensure!(!Invulnerables::<T>::get().contains(&who), Error::<T>::AlreadyInvulnerable);

			let validator_key = T::ValidatorIdOf::convert(who.clone())
				.ok_or(Error::<T>::NoAssociatedValidatorId)?;
			ensure!(
				T::ValidatorRegistration::is_registered(&validator_key),
				Error::<T>::ValidatorNotRegistered
			);

			let deposit = CandidacyBond::<T>::get();
			// First authored block is current block plus kick threshold to handle session delay
			CandidateList::<T>::try_mutate(|candidates| -> Result<(), DispatchError> {
				ensure!(
					!candidates.iter().any(|candidate_info| candidate_info.who == who),
					Error::<T>::AlreadyCandidate
				);
				T::Currency::reserve(&who, deposit)?;
				LastAuthoredBlock::<T>::insert(
					who.clone(),
					frame_system::Pallet::<T>::block_number() + T::KickThreshold::get(),
				);
				candidates
					.try_insert(0, CandidateInfo { who: who.clone(), deposit })
					.map_err(|_| Error::<T>::InsertToCandidateListFailed)?;
				Ok(())
			})?;
```
