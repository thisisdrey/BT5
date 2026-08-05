Found the analog: `pallet_nomination_pools::Pallet::apply_slash` and `pallet_delegated_staking::Pallet::do_slash` never verify that the slash reporter differs from the slashed member.

### Title
Pool member can self-report their own pending slash to reclaim `SlashRewardFraction` of their own penalty - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`apply_slash` in `pallet-nomination-pools` and the underlying `do_slash` in `pallet-delegated-staking` allow *any* signed account to apply a pending slash to a pool member and be rewarded `SlashRewardFraction` of the slashed amount. Neither function checks that `reporter != member_account` (or delegator). A slashed member can therefore call `apply_slash` on themselves as the reporter and immediately reclaim a percentage of the penalty that was just deducted from their own stake — mirroring the `BobStaking::instantWithdraw` pattern where a penalty is credited into a pot the payer can immediately reclaim.

### Finding Description
`Pallet::<T>::apply_slash` takes any signed origin and a `member_account` lookup, with no equality check between the two: [1](#0-0) 

It forwards to `do_apply_slash`, which passes the caller through as `reporter: Some(who)` unconditionally: [2](#0-1) 

This reaches `StakeAdapter::member_slash` → `DelegationInterface::delegator_slash` → `pallet_delegated_staking::Pallet::do_slash`, which slashes the delegator's held balance and then, if a reporter is supplied, splits off `SlashRewardFraction * actual_slash` and pays it directly to that reporter account: [3](#0-2) 

There is no assertion anywhere in this call chain that `delegator != reporter`. If the slashed member (or an account they control) supplies their own account as the caller of `apply_slash`, `credit.split(reward_payout)` still executes and `T::Currency::resolve(&reporter, reporter_reward)` pays the reward back to the very account that was just slashed — the member effectively pays their own penalty and immediately reclaims `SlashRewardFraction` of it, exactly as in the report's residual-recycling pattern (pay the fee into a pot, then drain it back via a different, unguarded code path).

The existing tests only exercise the "third-party reporter" case (`reporter: 400`, `slash_reporter: 99`), never verifying the self-report path is rejected: [4](#0-3) [5](#0-4) 

The `apply_slash` extrinsic is deliberately permissionless (`Pays::No` on success, intended to incentivize third parties to keep pending slashes applied), so nothing in the dispatch layer blocks the member from being their own caller.

### Impact Explanation
This is a direct value-conservation break in staking accounting: a percentage (`SlashRewardFraction`, configured per runtime) of every slash can be recycled back to the slashed party rather than being burned/handled by `T::OnSlash`. Since slashing is a security-critical, permissionless, unprivileged-attacker-accessible mechanism meant to punish stakers behind equivocating/offline validators, allowing the punished party to net-reduce their own penalty degrades the intended economic security guarantee of the whole delegated-staking/nomination-pools slashing design. It does not require a malicious validator, collator, relayer, or governance actor — only a normal pool member acting as their own slash reporter.

### Likelihood Explanation
High for any account that is both a pool delegator and can submit its own extrinsics (i.e., every pool member). `apply_slash` is permissionless by design (`origin` is simply `ensure_signed`), the reward fraction is paid unconditionally whenever `maybe_reporter` is `Some`, and no additional privilege or coordination is needed — the member just calls `apply_slash(RuntimeOrigin::signed(member), member_account_lookup)` on themselves once a pending slash exists.

### Recommendation
In `do_apply_slash` (`substrate/frame/nomination-pools/src/lib.rs`) and/or `do_slash` (`substrate/frame/delegated-staking/src/lib.rs`), explicitly reject or ignore the reporter reward when `reporter == Some(delegator)` (or more generally when the reporter is the slashed account itself or provably controlled by it, e.g. also disallow reporter == the pool's own accounts). Concretely: before computing `reward_payout`, check `if maybe_reporter.as_ref() == Some(&delegator) { None } else { maybe_reporter }` and drop the whole `credit` to `T::OnSlash` in that case instead of splitting off a reporter reward.

### Proof of Concept
1. Member `M` joins a pool and is later slashed (pending slash recorded via `on_slash`/`do_slash` bookkeeping in `pallet-staking`/`pallet-delegated-staking`), e.g. as shown by the `pending_slash` setup in the existing test `pool_partially_slashed`.
2. `M` calls `Pools::apply_slash(RuntimeOrigin::signed(M), M_lookup)`.
3. This dispatches to `do_apply_slash(&M, Some(M), true)` → `StakeAdapter::member_slash(..., Some(M))` → `DelegatedStaking::delegator_slash(agent, Delegator::from(M), pending_slash, Some(M))` → `do_slash` in `pallet-delegated-staking`.
4. `do_slash` slashes `M`'s held balance by `pending_slash`, then computes `reward_payout = SlashRewardFraction * actual_slash` and calls `T::Currency::resolve(&M, reporter_reward)` — crediting the reward straight back to `M`.
5. Net effect: `M`'s effective slash is `actual_slash * (1 - SlashRewardFraction)` instead of the full `actual_slash`, self-funded exactly like the `instantWithdraw`/`claimResidualRewards` recycling in the source report.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L3143-3160)
```rust
		#[pallet::call_index(23)]
		#[pallet::weight(T::WeightInfo::apply_slash())]
		pub fn apply_slash(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
		) -> DispatchResultWithPostInfo {
			ensure!(
				T::StakeAdapter::strategy_type() == adapter::StakeStrategyType::Delegate,
				Error::<T>::NotSupported
			);

			let who = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
			Self::do_apply_slash(&member_account, Some(who), true)?;

			// If successful, refund the fees.
			Ok(Pays::No.into())
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

**File:** substrate/frame/delegated-staking/src/lib.rs (L704-753)
```rust
	/// Take slash `amount` from agent's `pending_slash`counter and apply it to `delegator` account.
	pub fn do_slash(
		agent: Agent<T::AccountId>,
		delegator: Delegator<T::AccountId>,
		amount: BalanceOf<T>,
		maybe_reporter: Option<T::AccountId>,
	) -> DispatchResult {
		// get inner type
		let agent = agent.get();
		let delegator = delegator.get();

		let agent_ledger = AgentLedgerOuter::<T>::get(&agent)?;
		// ensure there is something to slash
		ensure!(agent_ledger.ledger.pending_slash > Zero::zero(), Error::<T>::NothingToSlash);

		let mut delegation = <Delegators<T>>::get(&delegator).ok_or(Error::<T>::NotDelegator)?;
		ensure!(delegation.agent == agent.clone(), Error::<T>::NotAgent);
		ensure!(delegation.amount >= amount, Error::<T>::NotEnoughFunds);

		// slash delegator
		let (mut credit, missing) =
			T::Currency::slash(&HoldReason::StakingDelegation.into(), &delegator, amount);

		defensive_assert!(missing.is_zero(), "slash should have been fully applied");

		let actual_slash = credit.peek();

		// remove the applied slashed amount from agent.
		agent_ledger.remove_slash(actual_slash).save();
		delegation.amount =
			delegation.amount.checked_sub(&actual_slash).ok_or(ArithmeticError::Overflow)?;
		delegation.update(&delegator);

		if let Some(reporter) = maybe_reporter {
			let reward_payout: BalanceOf<T> = T::SlashRewardFraction::get() * actual_slash;
			let (reporter_reward, rest) = credit.split(reward_payout);

			// credit is the amount that we provide to `T::OnSlash`.
			credit = rest;

			// reward reporter or drop it.
			let _ = T::Currency::resolve(&reporter, reporter_reward);
		}

		T::OnSlash::on_unbalanced(credit);

		Self::deposit_event(Event::<T>::Slashed { agent, delegator, amount });

		Ok(())
	}
```

**File:** substrate/frame/delegated-staking/src/tests.rs (L258-294)
```rust
		// ensure bookkept pending slash is correct.
		assert_eq!(get_agent_ledger(&agent).ledger.pending_slash, total_staked / 2);
		let mut old_reporter_balance = Balances::free_balance(reporter);

		// lets apply the pending slash on delegators.
		for i in delegators {
			// balance before slash
			let initial_pending_slash = get_agent_ledger(&agent).ledger.pending_slash;
			assert!(initial_pending_slash > 0);
			let unslashed_balance = DelegatedStaking::held_balance_of(Delegator::from(i));
			let slash = unslashed_balance / 2;
			// slash half of delegator's delegation.
			assert_ok!(<DelegatedStaking as DelegationInterface>::delegator_slash(
				Agent::from(agent),
				Delegator::from(i),
				slash,
				Some(400)
			));

			// balance after slash.
			assert_eq!(
				DelegatedStaking::held_balance_of(Delegator::from(i)),
				unslashed_balance - slash
			);
			// pending slash is reduced by the amount slashed.
			assert_eq!(
				get_agent_ledger(&agent).ledger.pending_slash,
				initial_pending_slash - slash
			);
			// reporter get 10% of the slash amount.
			assert_eq!(
				Balances::free_balance(reporter) - old_reporter_balance,
				<Staking as StakingInterface>::slash_reward_fraction() * slash,
			);
			// update old balance
			old_reporter_balance = Balances::free_balance(reporter);
		}
```

**File:** substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs (L742-762)
```rust
		// 21's balance in the pool is slashed.
		assert_eq!(PoolMembers::<Runtime>::get(21).unwrap().total_balance(), 7);
		// But their actual balance is still unslashed.
		assert_eq!(Balances::total_balance_on_hold(&21), bond);
		// 21 has pending slash
		assert_eq!(Pools::api_member_pending_slash(21), bond - 7);
		// apply slash permissionlessly.
		assert_ok!(Pools::apply_slash(RuntimeOrigin::signed(10), 21));
		// member balance is slashed.
		assert_eq!(Balances::total_balance_on_hold(&21), 7);
		// 21 has no pending slash anymore
		assert_eq!(Pools::api_member_pending_slash(21), 0);

		assert_eq!(
			delegated_staking_events_since_last_call(),
			vec![DelegatedStakingEvent::Slashed {
				agent: POOL1_BONDED,
				delegator: 21,
				amount: bond - 7
			}]
		);
```
