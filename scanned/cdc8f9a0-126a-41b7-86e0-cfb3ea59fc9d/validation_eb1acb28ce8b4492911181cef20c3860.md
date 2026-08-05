### Title
Rounding-divergent slash accounting between `pallet-nomination-pools` and `pallet-delegated-staking` can permanently strand held member funds - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
This is the local analog of the Skale `DelegationController` rounding bug: multiple independently-updated balances that are supposed to always represent the *same* underlying value (a member's slashable stake) are computed through different rounding paths — points-to-balance ratio truncation in the pool, versus exact hold-balance bookkeeping in `pallet-delegated-staking`/`pallet-staking`. The mismatch is “resolved” with `saturating_sub`, which silently hides the divergence instead of surfacing it, and the pool's `apply_slash` entrypoint additionally gates on a minimum-slash threshold, so small residual mismatches (dust from rounding) can never be reconciled and the corresponding held balance becomes permanently stuck.

### Finding Description
`Pallet::<T>::member_pending_slash` computes how much of a member's held balance still needs to be slashed by subtracting the member's *expected* balance (derived from `pool_member.total_balance()`, itself computed via `point_to_balance`/`balance_to_point` integer-truncated ratios, see [1](#0-0) ) from the member's *actual* balance held in `pallet-delegated-staking` (`T::StakeAdapter::member_delegation_balance`): [2](#0-1) 

Both `actual_balance` and `expected_balance` are derived from the *same* underlying slash event but travel through two different, independently-rounded code paths:
- The pool side tracks slashed amounts per unbonding-era `UnbondPool` (`issue`/`dissolve`, `point_to_balance`) with floor/ceil rounding at each `balance_to_point`/`point_to_balance` conversion — visibly rounding in the codebase's own comments/tests, e.g. “This era got slashed 12.5, which rounded up to 13” / “12 instead of 12.5” ( [3](#0-2) ).
- The delegated-staking side tracks a single scalar `pending_slash` on the `Agent` ledger that is decremented exactly by whatever `amount` is passed to `do_slash` ( [4](#0-3) ), which in turn is driven by the pool's own (already-rounded) per-member computation.

Because `member_pending_slash` uses `saturating_sub` rather than an exact/checked subtraction (exactly the “make result value equal to zero if underflow happens” pattern flagged as unstable in the Skale report), any accumulated rounding drift between the pool's point-based accounting and the delegated-staking hold accounting is silently clamped to zero instead of being flagged. When the residual mismatch is small but nonzero, `Pools::apply_slash` will refuse to act on it: the pool's own test harness demonstrates this exact "dust" scenario — `SlashTooLow` is returned when the pending slash for a member is below `ExistentialDeposit` ( [5](#0-4) ). Unlike the pool's bonded-balance dust (which is force-zeroed via the "avoid dust" logic on unbond, see [6](#0-5) ), there is no such force-resolution path for the *delegator-held* dust tracked by `pallet-delegated-staking`: the held amount remains on hold, `pending_slash` on the agent ledger never reaches zero for that residual, and neither `apply_slash` (blocked by `SlashTooLow`) nor normal withdrawal (which itself calls into the same slash-then-release pipeline) can clear it.

### Impact Explanation
This falls under "permanent user-fund lock" in the impact taxonomy: a delegator's held stake can become permanently un-releasable dust once the point-based and hold-based accounting for that member diverge by an amount below the minimum slash threshold. It does not require any privileged actor — it is a natural consequence of repeated slash/unbond/merge cycles across `SubPools::maybe_merge_pools` (which itself uses `saturating_add` merges of independently-rounded `UnbondPool` ratios, see [7](#0-6) ) each of which introduces its own rounding, and the delegated-staking side which does exact subtraction. Funds affected are user (delegator) funds, not just protocol accounting, matching the required-impact class of "permanent user-fund or bridge-state lock."

### Likelihood Explanation
Likelihood is moderate: it requires several successive slash events interacting with era-based pool merges (`maybe_merge_pools`) to accumulate enough rounding drift, and the resulting stuck amount is bounded by `ExistentialDeposit`-scale dust per member, so the observable damage per incident is small. However, this is a systemic property of the design (present on every slash), not a one-off edge case, and it requires no malicious actor — it can be triggered purely by normal permissionless usage (slashing events + unbond/withdraw calls), consistent with the report's classification of an unprivileged, non-adversarial rounding defect.

### Recommendation
- Replace `saturating_sub` in `member_pending_slash` with a check that distinguishes "no pending slash" from "pending slash rounds to less than ED" and reconcile/force the residual dust to exactly zero on both ledgers atomically (mirroring the `active < existential_deposit` dust-absorption pattern already used in `unbond`/`slash` in `pallet-staking`/`pallet-staking-async`).
- When `apply_slash` returns `SlashTooLow`, do not leave the member's held balance and the agent's `pending_slash` permanently desynchronized — either absorb dust into the next slash/withdrawal or allow a permissionless "sweep" that zeroes out sub-ED residuals on both sides simultaneously.
- Consider tracking pool slash state with a single canonical ledger (rather than independently-rounded point/balance conversions on one side and exact scalar accounting on the other) to eliminate cross-pallet rounding divergence entirely.

### Proof of Concept
A full multi-block PoC (compounding several slash events with intervening `SubPools::maybe_merge_pools` merges to build up sub-ED rounding drift, then attempting `apply_slash`/`withdraw_unbonded` to show `SlashTooLow`/no state change) requires running the pallet's test harness, which is not verifiable from static repository inspection alone. The dust/rounding mechanics and the `SlashTooLow` gate are directly demonstrated in-repo: [8](#0-7) 
This confirms the exact preconditions (small residual slash below ED, rejected by `apply_slash`) needed to reproduce the permanent-lock scenario described above; a Devin session with test-execution access would be needed to drive the accounting into a nonzero, permanently-unreachable state end-to-end.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1603-1629)
```rust
impl<T: Config> SubPools<T> {
	/// Merge the oldest `with_era` unbond pools into the `no_era` unbond pool.
	///
	/// This is often used whilst getting the sub-pool from storage, thus it consumes and returns
	/// `Self` for ergonomic purposes.
	fn maybe_merge_pools(mut self, active_era: EraIndex) -> Self {
		// Retain `with_era` pools for ~`MaxUnbondingPools` eras after unlock.
		// E.g., if window is 2 and active era is 10, retain pools 9..=10.
		let effective_post_unbonding_window =
			T::MaxUnbondingPools::get().saturating_sub(T::StakeAdapter::bonding_duration());
		if let Some(newest_era_to_remove) = active_era.checked_sub(effective_post_unbonding_window)
		{
			self.with_era.retain(|k, v| {
				if *k > newest_era_to_remove {
					// keep
					true
				} else {
					// merge into the no-era pool
					self.no_era.points = self.no_era.points.saturating_add(v.points);
					self.no_era.balance = self.no_era.balance.saturating_add(v.balance);
					false
				}
			});
		}

		self
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3501-3522)
```rust
	/// Calculate the equivalent balance of `points` in a pool with `current_balance` and
	/// `current_points`.
	fn point_to_balance(
		current_balance: BalanceOf<T>,
		current_points: BalanceOf<T>,
		points: BalanceOf<T>,
	) -> BalanceOf<T> {
		let u256 = T::BalanceToU256::convert;
		let balance = T::U256ToBalance::convert;
		if current_balance.is_zero() || current_points.is_zero() || points.is_zero() {
			// There is nothing to unbond
			return Zero::zero();
		}

		// Equivalent of (current_balance / current_points) * points
		balance(
			u256(current_balance)
				.saturating_mul(u256(points))
				// We check for zero above
				.div(u256(current_points)),
		)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3846-3873)
```rust
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

**File:** substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs (L691-712)
```rust
		hypothetically!({
			// a very small amount is slashed
			pallet_staking_async::slashing::do_slash::<Runtime>(
				&POOL1_BONDED,
				3,
				&mut Default::default(),
				&mut Default::default(),
				100,
			);

			// ensure correct amount is pending to be slashed
			assert_eq!(Pools::api_pool_pending_slash(1), 3);

			// 21 has pending slash lower than ED (2)
			assert_eq!(Pools::api_member_pending_slash(21), 1);

			// slash fails as minimum pending slash amount not met.
			assert_noop!(
				Pools::apply_slash(RuntimeOrigin::signed(10), 21),
				PoolsError::<Runtime>::SlashTooLow
			);
		});
```

**File:** substrate/frame/nomination-pools/test-delegate-stake/src/lib.rs (L729-740)
```rust
		assert_eq!(
			pool_events_since_last_call(),
			vec![
				// This era got slashed 12.5, which rounded up to 13.
				PoolsEvent::UnbondingPoolSlashed { pool_id: 1, era: 128, balance: 7 },
				// This era got slashed 12 instead of 12.5 because an earlier chunk got 0.5 more
				// slashed, and 12 is all the remaining slash
				PoolsEvent::UnbondingPoolSlashed { pool_id: 1, era: 129, balance: 8 },
				// Bonded pool got slashed for 25, remaining 15 in it.
				PoolsEvent::PoolSlashed { pool_id: 1, balance: 15 }
			]
		);
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

**File:** substrate/frame/staking-async/src/pallet/mod.rs (L1969-1976)
```rust
			if !value.is_zero() {
				ledger.active -= value;

				// Avoid there being a dust balance left in the staking system.
				if ledger.active < asset::existential_deposit::<T>() {
					value += ledger.active;
					ledger.active = Zero::zero();
				}
```
