### Title
Regulatory/compliance staking restriction (`Filter`/`Restricted`) is not enforced on the `pallet-delegated-staking` deposit path, allowing a restricted account to effectively stake anyway - (`File: substrate/frame/delegated-staking/src/lib.rs`)

### Summary
`pallet-staking-async` (and legacy `pallet-staking`) enforce a compliance filter (`T::Filter::contains`) on the direct `bond`/`bond_extra` extrinsics, rejecting restricted accounts with `Error::Restricted`. `pallet-nomination-pools::join`/`bond_extra` re-implements the same check for its members. However, `pallet-delegated-staking`, which is the "agent-mediated" alternate path into `CoreStaking` (used both standalone and as the backing adapter for pools' `DelegateStake` strategy), never consults any restriction/filter mechanism at all. A restricted delegator can call `delegate_to_agent` directly and have their funds staked (via `virtual_bond`/`bond_extra` on `CoreStaking`) under an `Agent`'s ledger, bypassing the exact same `Restricted` guard that blocks them from calling `Staking::bond`/`bond_extra` directly. This is the same class of bug as the report: a restriction is enforced on the "primary" deposit entrypoint but not on a secondary/composed entrypoint that ultimately performs the same privileged action (staking) on behalf of the restricted identity.

### Finding Description
`pallet-staking-async::Pallet::bond` and `::bond_extra` guard against restricted stashes: [1](#0-0) 

The compliance mechanism (`T::Filter`) was purpose-built to "restrict certain accounts from staking, for example, accounts staking via pools, and vice versa": [2](#0-1) 

`pallet-nomination-pools` re-checks the same filter on its own `join` entrypoint before delegating/transferring funds toward staking: [3](#0-2) 

and the delegated-staking integration tests confirm that a pool member who becomes staked (or vice versa) is rejected via the `Restricted` error surfaced from `CoreStaking`: [4](#0-3) 

However, `pallet-delegated-staking::delegate_to_agent` - the public dispatchable used when an account wants to stake through an `Agent` (either directly, or as the mechanism pools use under the `DelegateStake` adapter) - performs no restriction check on the `delegator`: [5](#0-4) 

It then proceeds to `do_delegate` (which only holds funds and updates the `AgentLedger`/`Delegation` records) and `do_bond`, which calls straight into `T::CoreStaking::bond_extra`/`T::CoreStaking::virtual_bond` — internal `StakingInterface` trait methods, not the `Call::bond`/`Call::bond_extra` extrinsics that carry the `T::Filter::contains` guard: [6](#0-5) 

A grep of the entire `pallet-delegated-staking` non-test/non-mock source shows zero references to a `Filter` trait or `Restricted` error anywhere in the pallet — it does not even define such a `Config` item, meaning there is structurally no place to enforce the restriction inside this pallet.

This is the exact analog of the reported bug: `StakediTry`'s `SOFT_RESTRICTED_STAKER_ROLE` check exists only in the direct staking path, while `wiTryVaultComposer._deposit()` calls `VAULT.deposit()` on behalf of the composer/depositor and never re-verifies the depositor's restriction, letting the restricted user stake indirectly. Here, `pallet-staking-async::bond`/`bond_extra` enforce `Restricted`, while `pallet-delegated-staking::delegate_to_agent` performs the same underlying action (increasing bonded/staked balance backing an account in `CoreStaking`) on behalf of the delegator without ever checking that delegator's restricted status.

### Impact Explanation
This breaks an intended regulatory/compliance control (`T::Filter`) that a runtime configures specifically to block certain accounts (e.g., sanctioned or non-compliant addresses) from participating in staking. A restricted account can still economically stake — earning staking rewards and influencing an agent's total stake/voting power — purely by routing through `pallet-delegated-staking::delegate_to_agent` instead of `pallet-staking-async::bond`. This is a runtime bug that compromises intended behavior of a security/compliance control that governance explicitly wired into the staking pallet, matching the "runtime bugs that compromise intended behavior" acceptance criteria.

### Likelihood Explanation
High likelihood on any runtime where `pallet-delegated-staking` is deployed standalone or as a `DelegateStake` adapter backend and a `Filter`-based restriction list is configured on `pallet-staking-async`/`pallet-staking`. The bypass requires only an unprivileged signed call to a public extrinsic (`delegate_to_agent`) and a pre-registered `Agent` (which itself can be created permissionlessly via `register_agent`). No malicious validator, collator, relayer, or admin action is required — the restricted user alone can execute the entire bypass.

### Recommendation
Add the same restriction check used by `pallet-staking-async::bond`/`bond_extra` and `pallet-nomination-pools::join` to `pallet-delegated-staking::delegate_to_agent` (and to `do_bond_extra`/`delegate_extra` paths), i.e. introduce a `Filter` associated type on `pallet-delegated-staking::Config` and `ensure!(!T::Filter::contains(&delegator), Error::<T>::Restricted)` before `do_delegate`/`do_bond` are invoked. Alternatively, have `CoreStaking::virtual_bond`/`bond_extra` itself consult the filter for the ultimate economic staker (the delegator), not just the on-chain stash used for bonding.

### Proof of Concept
1. Governance/root adds `alice` to the compliance restriction list backing `T::Filter` (as done in `substrate/frame/staking-async/src/tests/bonding.rs::restricted_accounts_can_only_withdraw`, using the `restrict(&alice)` helper).
2. `alice` attempts `Staking::bond(RuntimeOrigin::signed(alice), 100, RewardDestination::Staked)` → fails with `Error::<T>::Restricted` (per `substrate/frame/staking-async/src/pallet/mod.rs:1859`).
3. `bob` calls `DelegatedStaking::register_agent(RawOrigin::Signed(bob).into(), bob_reward_account)`.
4. `alice` calls `DelegatedStaking::delegate_to_agent(RawOrigin::Signed(alice).into(), bob, 100)`.
5. This succeeds: `Delegation::can_delegate` only checks that `alice` isn't already delegating elsewhere and isn't an agent herself (`substrate/frame/delegated-staking/src/lib.rs:436-440`), then `do_delegate` holds `alice`'s funds and `do_bond` calls `T::CoreStaking::virtual_bond(&bob, 100, ...)`, bonding the funds into `CoreStaking` under `bob`'s ledger — with no `Restricted` check performed anywhere in the call path.
6. `alice`, despite being on the restriction list, now has an economically staked position via `bob`'s agent ledger, mirroring the wiTry composer bypass.

### Citations

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1852-1905)
```rust
		pub fn bond(
			origin: OriginFor<T>,
			#[pallet::compact] value: BalanceOf<T>,
			payee: RewardDestination<T::AccountId>,
		) -> DispatchResult {
			let stash = ensure_signed(origin)?;

			ensure!(!T::Filter::contains(&stash), Error::<T>::Restricted);

			if StakingLedger::<T>::is_bonded(StakingAccount::Stash(stash.clone())) {
				return Err(Error::<T>::AlreadyBonded.into());
			}

			// An existing controller cannot become a stash.
			if StakingLedger::<T>::is_bonded(StakingAccount::Controller(stash.clone())) {
				return Err(Error::<T>::AlreadyPaired.into());
			}

			// Reject a bond which is lower than the minimum bond.
			if value < Self::min_chilled_bond() {
				return Err(Error::<T>::InsufficientBond.into());
			}

			let stash_balance = asset::free_to_stake::<T>(&stash);
			let value = value.min(stash_balance);
			Self::deposit_event(Event::<T>::Bonded { stash: stash.clone(), amount: value });
			let ledger = StakingLedger::<T>::new(stash.clone(), value);

			// You're auto-bonded forever, here. We might improve this by only bonding when
			// you actually validate/nominate and remove once you unbond __everything__.
			ledger.bond(payee)?;

			Ok(())
		}

		/// Add some extra amount that have appeared in the stash `free_balance` into the balance up
		/// for staking.
		///
		/// The dispatch origin for this call must be _Signed_ by the stash, not the controller.
		///
		/// Use this if there are additional funds in your stash account that you wish to bond.
		/// Unlike [`bond`](Self::bond) or [`unbond`](Self::unbond) this function does not impose
		/// any limitation on the amount that can be added.
		///
		/// Emits `Bonded`.
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::bond_extra())]
		pub fn bond_extra(
			origin: OriginFor<T>,
			#[pallet::compact] max_additional: BalanceOf<T>,
		) -> DispatchResult {
			let stash = ensure_signed(origin)?;
			ensure!(!T::Filter::contains(&stash), Error::<T>::Restricted);
			Self::do_bond_extra(&stash, max_additional)
```

**File:** prdoc/stable2503/pr_7685.prdoc (L1-20)
```text
title: 'Introduce filters to restrict accounts from staking'

doc:
  - audience: Runtime Dev
    description: |
      Introduce filters to restrict accounts from staking.
      This is useful for restricting certain accounts from staking, for example, accounts staking via pools, and vice
      versa.

crates:
  - name: pallet-staking
    bump: minor
  - name: pallet-nomination-pools
    bump: minor
  - name: westend-runtime
    bump: patch
  - name: pallet-delegated-staking
    bump: patch
  - name: pallet-nomination-pools-benchmarking
    bump: patch
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2120-2135)
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

```

**File:** substrate/frame/delegated-staking/src/tests.rs (L1327-1379)
```rust
	#[test]
	fn existing_pool_member_cannot_stake() {
		// A pool member is able to stake directly since staking only uses free funds but once a
		// staker, they cannot join/add extra bond to the pool. They can still withdraw funds.
		ExtBuilder::default().build_and_execute(|| {
			start_era(1);
			// GIVEN: a pool.
			fund(&200, 1000);
			let pool_id = create_pool(200, 800);

			// WHEN: delegator joins a pool
			let delegator = 100;
			fund(&delegator, 1000);
			assert_ok!(Pools::join(RawOrigin::Signed(delegator).into(), 200, pool_id));

			// THEN: they cannot stake anymore
			assert_noop!(
				Staking::bond(
					RuntimeOrigin::signed(delegator),
					500,
					RewardDestination::Account(101)
				),
				StakingError::<T>::Restricted
			);
		});
	}

	#[test]
	fn stakers_cannot_join_pool() {
		ExtBuilder::default().build_and_execute(|| {
			start_era(1);
			// GIVEN: a pool.
			fund(&200, 1000);
			let pool_id = create_pool(200, 800);

			// WHEN: an account is a staker.
			let staker = 100;
			fund(&staker, 1000);

			assert_ok!(Staking::bond(
				RuntimeOrigin::signed(staker),
				500,
				RewardDestination::Account(101)
			));
			assert_ok!(Staking::nominate(RuntimeOrigin::signed(staker), vec![GENESIS_VALIDATOR]));

			// THEN: they cannot join pool.
			assert_noop!(
				Pools::join(RawOrigin::Signed(staker).into(), 200, pool_id),
				PoolsError::<T>::Restricted
			);
		});
	}
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L429-450)
```rust
		pub fn delegate_to_agent(
			origin: OriginFor<T>,
			agent: T::AccountId,
			amount: BalanceOf<T>,
		) -> DispatchResult {
			let delegator = ensure_signed(origin)?;

			// ensure delegator is sane.
			ensure!(
				Delegation::<T>::can_delegate(&delegator, &agent),
				Error::<T>::InvalidDelegation
			);

			// ensure agent is sane.
			ensure!(Self::is_agent(&agent), Error::<T>::NotAgent);

			// add to delegation.
			Self::do_delegate(Delegator::from(delegator), Agent::from(agent.clone()), amount)?;

			// bond the newly delegated amount to `CoreStaking`.
			Self::do_bond(Agent::from(agent), amount)
		}
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L549-598)
```rust
	/// Bond `amount` to `agent_acc` in [`Config::CoreStaking`].
	fn do_bond(agent_acc: Agent<T::AccountId>, amount: BalanceOf<T>) -> DispatchResult {
		let agent_ledger = AgentLedgerOuter::<T>::get(&agent_acc.get())?;

		let available_to_bond = agent_ledger.available_to_bond();
		defensive_assert!(amount == available_to_bond, "not expected value to bond");

		if agent_ledger.is_bonded() {
			T::CoreStaking::bond_extra(&agent_ledger.key, amount)
		} else {
			T::CoreStaking::virtual_bond(&agent_ledger.key, amount, agent_ledger.reward_account())
		}
	}

	/// Delegate `amount` from `delegator` to `agent`.
	fn do_delegate(
		delegator: Delegator<T::AccountId>,
		agent: Agent<T::AccountId>,
		amount: BalanceOf<T>,
	) -> DispatchResult {
		// get inner type
		let agent = agent.get();
		let delegator = delegator.get();

		let mut ledger = AgentLedger::<T>::get(&agent).ok_or(Error::<T>::NotAgent)?;

		if let Some(mut existing_delegation) = Delegation::<T>::get(&delegator) {
			ensure!(existing_delegation.agent == agent, Error::<T>::InvalidDelegation);
			// update amount and return the updated delegation.
			existing_delegation.amount = existing_delegation
				.amount
				.checked_add(&amount)
				.ok_or(ArithmeticError::Overflow)?;
			existing_delegation
		} else {
			Delegation::<T>::new(&agent, amount)
		}
		.update(&delegator);

		// try to hold the funds.
		T::Currency::hold(&HoldReason::StakingDelegation.into(), &delegator, amount)?;

		ledger.total_delegated =
			ledger.total_delegated.checked_add(&amount).ok_or(ArithmeticError::Overflow)?;
		ledger.update(&agent);

		Self::deposit_event(Event::<T>::Delegated { agent, delegator, amount });

		Ok(())
	}
```
