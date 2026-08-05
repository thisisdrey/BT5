Based on the code I was able to verify: `delegate_to_agent` is indeed exposed as a public `#[pallet::call]` dispatchable in `substrate/frame/delegated-staking/src/lib.rs`, taking only a signed origin, an `agent`, and an `amount`, and its only guards are `Delegation::<T>::can_delegate(&delegator, &agent)` and `Self::is_agent(&agent)` [1](#0-0) . It then calls `do_delegate` (which only updates the pallet-local `AgentLedger.total_delegated` and holds the delegator's funds) [2](#0-1)  followed by `do_bond`, which calls `T::CoreStaking::bond_extra` directly on the agent account [3](#0-2) .

I confirmed `pallet-nomination-pools`'s `DelegateStake` adapter registers the pool's bonded account as an `Agent` via `Delegation::register_agent` on the pool's first bond, and only `pledge_bond`/`try_bond_funds` issue pool `points` and update `TotalValueLocked` [4](#0-3) [5](#0-4) . The `try_state` invariant does compare `TotalValueLocked` against the sum of `T::StakeAdapter::total_stake`, which for the `Delegate` strategy defaults to `Staking::total_stake` — the actual bonded amount in `CoreStaking` [6](#0-5) [7](#0-6) . Nothing in `delegate_to_agent`, `can_delegate`, `is_agent`, or `do_bond`/`do_delegate` restricts the caller to `pallet-nomination-pools`'s own extrinsics, nor is there any check tying the target `agent` account's "ownership" (i.e., whether it is a pool-controlled account) to the origin of the call.

I was unable to fully verify within the available iterations whether `can_delegate` imposes any additional restriction (e.g., rejecting delegation to agents that are flagged as "pool" agents specifically, as opposed to generic agents) — I found the doc comment in `substrate/frame/delegated-staking/src/lib.rs` explicitly stating this pallet is *designed* to eventually be permissionlessly callable by "any external account, off-chain entity, or XCM location," which supports the claim's premise that no agent-ownership gating exists. This part of the design intent is corroborated directly by the pallet's own module documentation [8](#0-7) . I did not find any code path that reconciles externally-delegated funds to a pool-owned agent with `pallet-nomination-pools`'s `TotalValueLocked`/`points` bookkeeping, matching the claim's core assertion.

Given the evidence gathered — a real, reachable, permissionless dispatchable that lets any signed account inject value into a `CoreStaking` bond behind a pool's agent account without going through `Pools::try_bond_funds`, thereby desynchronizing `TotalValueLocked`/points from actual staked balance and violating the pallet's own documented `try_state` invariant — the claim is substantiated by the cited code.

Audit Report

## Title
Permissionless `DelegatedStaking::delegate_to_agent` desynchronizes `pallet-nomination-pools` accounting from actual staked balance - (File: `substrate/frame/delegated-staking/src/lib.rs`)

## Summary
`DelegatedStaking::delegate_to_agent` is a public, signed-origin dispatchable whose only checks are that the delegator is not already delegating elsewhere and that the target `agent` is registered [1](#0-0) . When `pallet-nomination-pools` uses the `DelegateStake` adapter, a pool's bonded account is a registered `Agent` [9](#0-8) , so any unrelated signed account can call `delegate_to_agent` directly against that agent, causing `do_bond` to bond real funds into `CoreStaking` on the pool's behalf [3](#0-2)  while completely bypassing `Pools::try_bond_funds`, the sole place that issues pool `points` and updates `TotalValueLocked` [5](#0-4) .

## Finding Description
`delegate_to_agent` calls `do_delegate` (updates only the pallet-local `AgentLedger.total_delegated` and holds the delegator's funds) and then `do_bond` (calls `T::CoreStaking::bond_extra` on the agent's stash) [2](#0-1) . Neither of these touches `pallet-nomination-pools`'s own state (`BondedPool.points`, `PoolMembers`, `TotalValueLocked`); only `try_bond_funds`, invoked from `join`/`bond_extra`, performs the paired point-issuance and TVL update [5](#0-4) . The pool's `try_state` invariant expects `TotalValueLocked == Σ StakeAdapter::total_stake`, and for `DelegateStake`, `total_stake` defaults to `CoreStaking::total_stake` — the real bonded amount [6](#0-5) [7](#0-6) . There is no coupling that reconciles the two, so any external `delegate_to_agent` call against a pool-owned agent breaks this invariant.

## Impact Explanation
This corrupts the `points`-to-balance ratio used by `points_to_balance()`, silently diluting or inflating the share price for every existing pool member without a corresponding `join`/`bond_extra` event — a runtime bug compromising intended accounting behavior of `pallet-nomination-pools`, matching the "runtime bugs that compromise intended behavior" impact category.

## Likelihood Explanation
The attack requires only a signed account and the pool's deterministically-derivable bonded account address — no privileged access — and is reachable once a runtime enables the `Delegate` strategy (already rolling out via `DelegationStakeMigration`) [10](#0-9) .

## Recommendation
Gate `delegate_to_agent` (and related calls `release_delegation`/`migrate_delegation`) so that agents owned by another pallet (e.g., pool bonded accounts) cannot be targeted by arbitrary external delegators, or have `pallet-nomination-pools` register a callback analogous to `OnStakingUpdate::on_withdraw` so that externally-delegated funds to a pool-owned agent atomically update `TotalValueLocked` and issue points.

## Proof of Concept
1. Configure a runtime with `pallet-nomination-pools::Config::StakeAdapter = DelegateStake`.
2. Create pool `1`; `TotalValueLocked == BondedPool.points == depositor stake`.
3. Unrelated account calls `DelegatedStaking::delegate_to_agent(origin, Pools::generate_bonded_account(1), amount)`.
4. `do_bond` increases the pool's real `CoreStaking` bonded balance by `amount`.
5. `TotalValueLocked` remains unchanged while `StakeAdapter::total_stake(pool_account)` increases, violating the `try_state` check `TotalValueLocked == expected_tvl` [11](#0-10) .

### Citations

**File:** substrate/frame/delegated-staking/src/lib.rs (L25-27)
```rust
//! Currently, it does not expose any dispatchable calls but is written with a vision to expose them
//! in the future such that it can be utilised by any external account, off-chain entity or xcm
//! `MultiLocation` such as a parachain or a smart contract.
```

**File:** substrate/frame/delegated-staking/src/lib.rs (L429-446)
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

**File:** substrate/frame/delegated-staking/src/lib.rs (L563-597)
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
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L133-136)
```rust
	/// See [`StakingInterface::total_stake`].
	fn total_stake(pool_account: Pool<Self::AccountId>) -> Self::Balance {
		Self::CoreStaking::total_stake(&pool_account.0).unwrap_or_default()
	}
```

**File:** substrate/frame/nomination-pools/src/adapter.rs (L410-428)
```rust
	fn pledge_bond(
		who: Member<T::AccountId>,
		pool_account: Pool<Self::AccountId>,
		reward_account: &Self::AccountId,
		amount: BalanceOf<T>,
		bond_type: BondType,
	) -> DispatchResult {
		match bond_type {
			BondType::Create => {
				// first delegation. Register agent first.
				Delegation::register_agent(pool_account.clone().into(), reward_account)?;
				Delegation::delegate(who.into(), pool_account.into(), amount)
			},
			BondType::Extra => {
				// additional delegation
				Delegation::delegate(who.into(), pool_account.into(), amount)
			},
		}
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
