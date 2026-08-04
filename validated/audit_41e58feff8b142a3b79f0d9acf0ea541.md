This confirms the exact analog: `DelegatedStaking::delegate_to_agent` is a **public, permissionless extrinsic** callable by any signed account. It calls `Delegation::<T>::can_delegate(&delegator, &agent)` and `Self::is_agent(&agent)` — the *only* checks are "delegator not already delegating elsewhere" and "agent is registered" — there is no check that the caller is going through `pallet-nomination-pools`'s `join()`/`bond_extra()` entry points. When `nomination-pools` uses the `Delegate` staking strategy (`adapter.rs`, `migrate_to_transfer_stake`, `DelegationStakeMigration`), a pool's bonded account is registered as a `DelegatedStaking` **Agent** [1](#0-0) . Any outside account can then call `delegate_to_agent(origin, pool_bonded_account, amount)` directly [2](#0-1) , which holds the caller's funds and immediately bonds them into `CoreStaking` on behalf of the pool's agent account via `do_bond` [3](#0-2)  — completely bypassing `Pools::try_bond_funds()`, which is the only place that mints pool `points` and updates `TotalValueLocked` [4](#0-3) .

### Title
Permissionless `DelegatedStaking::delegate_to_agent` desynchronizes `pallet-nomination-pools` accounting from actual staked balance - (File: `substrate/frame/delegated-staking/src/lib.rs`)

### Summary
This is a structural analog of the `UltraLRT`/`AffineDelegator` bug: the vault (`pallet-nomination-pools`) keeps its own accounting (`BondedPool.points`, `PoolMembers`, `TotalValueLocked`) that is only updated through its own dispatchables (`join`, `bond_extra`, `unbond`, `withdraw_unbonded`). The lower-level "delegator" component (`pallet-delegated-staking`) exposes public dispatchables (`delegate_to_agent`, `release_delegation`, `migrate_delegation`) that operate directly on any registered `Agent` — including a pool's bonded account — without any coupling back to the pool's bookkeeping.

### Finding Description
When a pool uses the `StakeStrategyType::Delegate` adapter, the pool's bonded account is registered as a `DelegatedStaking::Agent` [5](#0-4) . `delegate_to_agent` only validates that the delegator isn't already delegating elsewhere and that the target is a registered agent [6](#0-5) ; it has no notion of, or guard against, the caller being an unrelated third party rather than `pallet-nomination-pools` itself. It then calls `do_delegate` (updates only `AgentLedger.total_delegated`, a `delegated-staking`-local value) and `do_bond` (which calls `CoreStaking::bond_extra` on the agent/pool stash directly) [7](#0-6) .

None of `nomination-pools`'s own state — `BondedPool.points`, `PoolMembers`, or `TotalValueLocked` — is touched by this path. Only `try_bond_funds`, invoked from `join`/`bond_extra`, performs the paired point-issuance and `TotalValueLocked` update [4](#0-3) . The `try_state` invariant explicitly expects `TotalValueLocked == sum(StakeAdapter::total_stake)` across all pools [8](#0-7) , which this path breaks: the real staked balance behind the pool grows while the pool-internal `TotalValueLocked`/points ledger does not.

### Impact Explanation
This corrupts the value `points_to_balance()` uses to compute share price. Since `try_bond_funds` is the sole place that issues points *before* transferring funds, any balance added to the agent's stake outside of it (via `delegate_to_agent`) is real staked value with no corresponding points — silently diluting/inflating the balance-per-point ratio for every existing pool member without any corresponding `join`/`bond_extra` event. The `try-state` check (`pr_5465.prdoc`, `TotalValueLocked deviates from the actual sum`) would fail on a live chain if this path is exercised, and the funds delegated this way become effectively unattributed to any `PoolMember`, are locked behind the pool's staking/unbonding mechanics, and can only be released back to their original delegator (not the pool depositor) via `release_delegation`, which is restricted to the agent account — i.e., only the pool's own signing key/proxy can normally trigger it, but the deposited principal already silently entered the pool's bonded stash and distorts `points_to_balance` for all members in the interim.

### Likelihood Explanation
The scenario requires only a signed account and knowledge of a pool's bonded-account address (`Pools::generate_bonded_account(pool_id)`, a public deterministic derivation) — no privileged, governance, or validator access is needed. It is directly reachable once a runtime enables the `Delegate` strategy for nomination pools (already being rolled out via `DelegationStakeMigration` in `migration.rs`) [9](#0-8) .

### Recommendation
`delegate_to_agent` (and `release_delegation`/`migrate_delegation`) should not permit a foreign `delegator` origin to alter the balance backing an `Agent` that is owned by another pallet (`nomination-pools`) unless invoked through that pallet's own extrinsics. Either (a) gate `Agent` registration so pool-owned agents reject external `delegate_to_agent` calls unless the caller is `pallet-nomination-pools`'s pallet-controlled call, or (b) have `nomination-pools` register a callback/hook analogous to `OnStakingUpdate::on_withdraw`/`on_slash` so that any external delegation to a pool-owned agent is reflected atomically in `TotalValueLocked` and issues points to the delegator, closing the accounting gap the same way the `on_withdraw` hook closed the implicit-withdrawal gap (`pr_3052.prdoc`).

### Proof of Concept
1. Configure a runtime where `pallet-nomination-pools::Config::StakeAdapter` uses `StakeStrategyType::Delegate`, so pool bonded accounts are `DelegatedStaking` agents.
2. Create pool `1` with depositor `10`; note `TotalValueLocked == 10` and `BondedPool.points == 10`.
3. An unrelated account `99` calls `DelegatedStaking::delegate_to_agent(origin=99, agent=Pools::generate_bonded_account(1), amount=1000)` directly (never calling `Pools::join`).
4. `do_bond` invokes `CoreStaking::bond_extra` on the pool's stash, increasing its real staked balance by 1000.
5. Observe `TotalValueLocked::<T>::get()` is unchanged (still 10) while `T::StakeAdapter::total_stake(pool_account)` is now 1010 — violating the pool's `try_state` invariant `TotalValueLocked == expected_tvl` [10](#0-9) , and every existing member's `points_to_balance` ratio is now computed against a stash balance that does not match the pool's internal points ledger.

### Citations

**File:** substrate/frame/nomination-pools/benchmarking/src/inner.rs (L134-161)
```rust
fn migrate_to_transfer_stake<T: Config>(pool_id: PoolId) {
	if T::StakeAdapter::strategy_type() == StakeStrategyType::Transfer {
		// should already be in the correct strategy
		return;
	}
	let pool_acc = Pools::<T>::generate_bonded_account(pool_id);
	// drop the agent and its associated delegators .
	T::StakeAdapter::remove_as_agent(Pool::from(pool_acc.clone()));

	// tranfer funds from all members to the pool account.
	PoolMembers::<T>::iter()
		.filter(|(_, member)| member.pool_id == pool_id)
		.for_each(|(member_acc, member)| {
			let member_balance = member.total_balance();
			<T as pallet_nomination_pools::Config>::Currency::transfer(
				&member_acc,
				&pool_acc,
				member_balance,
				Preservation::Preserve,
			)
			.expect("member should have enough balance to transfer");
		});

	// Pool needs to have ED balance free to stake so give it some.
	// Note: we didn't require ED until pallet-staking migrated from locks to holds.
	let _ = CurrencyOf::<T>::mint_into(&pool_acc, CurrencyOf::<T>::minimum_balance());

	pallet_staking_async::Pallet::<T>::migrate_to_direct_staker(&pool_acc);
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

**File:** substrate/frame/delegated-staking/src/lib.rs (L549-561)
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
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L563-598)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1308-1336)
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L4057-4086)
```rust
			ensure!(
				bonded_pool.points >= bonded_pool.points_to_balance(bonded_pool.points),
				"Each `BondedPool.points` must never be lower than the pool's balance"
			);

			expected_tvl += T::StakeAdapter::total_stake(Pool::from(bonded_pool.bonded_account()));

			Ok(())
		})?;

		if depositor_undermin_total > 0 {
			log!(
				warn,
				"{}/{} pools have depositor with insufficient stake, minimum required is {:?}. Examples: {:?}",
				depositor_undermin_total,
				total_pools,
				MinCreateBond::<T>::get(),
				depositor_undermin,
			);
		}

		ensure!(
			MaxPoolMembers::<T>::get().map_or(true, |max| all_members <= max),
			Error::<T>::MaxPoolMembers
		);

		ensure!(
			TotalValueLocked::<T>::get() == expected_tvl,
			"TVL deviates from the actual sum of funds of all Pools."
		);
```

**File:** substrate/frame/nomination-pools/src/migration.rs (L110-126)
```rust
	/// Migrate existing pools from [`adapter::StakeStrategyType::Transfer`] to
	/// [`adapter::StakeStrategyType::Delegate`].
	///
	/// Note: This only migrates the pools, the members are not migrated. They can use the
	/// permission-less [`Pallet::migrate_delegation()`] to migrate their funds.
	///
	/// This migration does not break any existing pool storage item, does not need to happen in any
	/// sequence and hence can be applied unversioned on a production runtime.
	///
	/// Takes `MaxPools` as type parameter to limit the number of pools that should be migrated in a
	/// single block. It should be set such that migration weight does not exceed the block weight
	/// limit. If all pools can be safely migrated, it is good to keep this number a little higher
	/// than the actual number of pools to handle any extra pools created while the migration is
	/// proposed, and before it is executed.
	///
	/// If there are pools that fail to migrate or did not fit in the bounds, the remaining pools
	/// can be migrated via the permission-less extrinsic [`Call::migrate_pool_to_delegate_stake`].
```
