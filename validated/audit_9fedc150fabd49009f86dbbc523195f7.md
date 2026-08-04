### Title
Slashed pool member can self-report and pocket the reporter's slash-reward cut via `apply_slash` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools::apply_slash` is a permissionless dispatchable that lets *any* signed account apply a pending slash to a pool member and be rewarded a cut of that slash as a "reporter" fee. There is no check that the caller (`who`) is different from the member being slashed (`member_account`). A member with a pending slash can call `apply_slash` on themselves, becoming their own reporter, and reclaim part of the value that is supposed to be a third-party incentive/whistleblower reward — mirroring the Astaria `canLiquidate()` bug where a borrower could self-liquidate and capture the liquidation bonus meant to reward an honest third party.

### Finding Description
`apply_slash` is defined as an explicitly permissionless call: [1](#0-0) 

It only validates the `StakeAdapter` strategy and looks up `member_account` via `T::Lookup`; it never asserts `who != member_account`:

```rust
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
    Ok(Pays::No.into())
}
```

`do_apply_slash` forwards `who` as `maybe_reporter` into `delegated-staking::do_slash`, which pays the reporter a `SlashRewardFraction` cut taken directly out of the slashed amount: [2](#0-1) 

```rust
if let Some(reporter) = maybe_reporter {
    let reward_payout: BalanceOf<T> = T::SlashRewardFraction::get() * actual_slash;
    let (reporter_reward, rest) = credit.split(reward_payout);
    credit = rest;
    let _ = T::Currency::resolve(&reporter, reporter_reward);
}
```

There is no origin/identity check preventing `member_account == who`. If a member with a pending slash calls `apply_slash(origin: signed(member), member_account: member)`, the flow: (1) slashes the member's held balance by `amount`, (2) computes `reward_payout = SlashRewardFraction * actual_slash`, (3) pays that reward back to the same account being slashed. This directly reduces the effective net slash the offending member suffers by `SlashRewardFraction` and diverts funds meant for an independent reporter/incentive back to the wrongdoer — the same "self-referential privileged action" pattern as the Astaria `canLiquidate()` bug, where the borrower could name themselves the liquidator and capture the incentive fee meant for a neutral party.

Additionally, the call sets `Pays::No`, meaning the self-slasher pays no transaction fee for performing this self-serving action, further reducing the cost/benefit ratio of exploiting it.

### Impact Explanation
This breaks the intended invariant that slash-reporting incentives go to a distinct, honest third party who takes on the gas/complexity cost of triggering the slash application, not to the offender itself. By allowing self-application, a slashed member effectively claws back part of their own penalty (`SlashRewardFraction * actual_slash`), undermining the punitive/economic-security purpose of slashing in delegated staking and causing an accounting mismatch: the value taken from `T::OnSlash` (e.g., treasury/burn) is smaller than intended, and the offender retains funds they should have lost. This is a value-conservation and "correct beneficiary" violation directly in scope (staking/asset accounting, reward payouts).

### Likelihood Explanation
High likelihood: `apply_slash` is a public, permissionless, unprivileged extrinsic requiring only a pending slash to exist on the caller's own account (a state any staked/delegated member can reach through normal validator slashing) and `Pays::No` removes even the fee deterrent. No governance, admin, relayer, or privileged actor is needed — an ordinary member simply calls the extrinsic naming themselves.

### Recommendation
In `apply_slash` (and the underlying `do_apply_slash` / `delegated-staking::do_slash` reporter logic), disallow paying the reporter reward when `who == member_account` (i.e., do not treat the slashed account as its own reporter), or more generally only pay the reporter fee when the caller is not the account being slashed.

### Proof of Concept
1. Member `M` delegates/bonds into a nomination pool using the `Delegate` stake strategy; a slash is later posted against `M`'s agent, creating `PendingSlash > 0` for `M` (via normal validator misbehavior/offence reporting).
2. `M` calls `NominationPools::apply_slash(origin: signed(M), member_account: M)`.
3. Inside `do_apply_slash` → `delegated_staking::do_slash(agent, delegator=M, amount, maybe_reporter=Some(M))`:
   - `M`'s held balance is slashed by `amount`.
   - `reward_payout = SlashRewardFraction * amount` is immediately resolved back to `M`.
4. Net effect: `M`'s effective slash is `amount * (1 - SlashRewardFraction)` instead of the full `amount`, and `M` paid no transaction fee (`Pays::No`) to perform this — directly analogous to the borrower in the Astaria report self-liquidating to capture the liquidation bonus meant for an independent party. [3](#0-2)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L3134-3160)
```rust
		/// Apply a pending slash on a member.
		///
		/// Fails unless [`crate::pallet::Config::StakeAdapter`] is of strategy type:
		/// [`adapter::StakeStrategyType::Delegate`].
		///
		/// The pending slash amount of the member must be equal or more than `ExistentialDeposit`.
		/// This call can be dispatched permissionlessly (i.e. by any account). If the execution
		/// is successful, fee is refunded and caller may be rewarded with a part of the slash
		/// based on the [`crate::pallet::Config::StakeAdapter`] configuration.
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

**File:** substrate/frame/nomination-pools/src/tests.rs (L1273-1325)
```rust
					Event::Bonded { member: 11, pool_id: 1, bonded: 11, joined: true },
					Event::Unbonded { member: 11, pool_id: 1, points: 11, balance: 11, era: 3 }
				]
			);
		});
	}

	#[test]
	fn claim_payout_bounds_commission_above_global() {
		ExtBuilder::default().build_and_execute(|| {
			let (mut member, bonded_pool, mut reward_pool) =
				Pools::get_member_with_pools(&10).unwrap();

			// top up commission payee account to existential deposit
			let _ = Currency::set_balance(&2, 5);

			// Set a commission pool 1 to 75%, with a payee set to `2`
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(900),
				bonded_pool.id,
				Some((Perbill::from_percent(75), 2)),
			));

			// re-introduce the global maximum to 50% - 25% lower than the current commission of the
			// pool.
			GlobalMaxCommission::<Runtime>::set(Some(Perbill::from_percent(50)));

			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 10, pool_id: 1 },
					Event::Bonded { member: 10, pool_id: 1, bonded: 10, joined: true },
					Event::MetadataUpdated { pool_id: 1, caller: 900 },
					Event::PoolCommissionUpdated {
						pool_id: 1,
						current: Some((Perbill::from_percent(75), 2))
					}
				]
			);

			// The pool earns 10 points
			deposit_rewards(10);

			assert_ok!(Pools::do_reward_payout(
				&10,
				&mut member,
				&mut BondedPool::<Runtime>::get(1).unwrap(),
				&mut reward_pool
			));

			// commission applied is 50%, not 75%. Has been bounded by `GlobalMaxCommission`.
			assert_eq!(
				pool_events_since_last_call(),
```
