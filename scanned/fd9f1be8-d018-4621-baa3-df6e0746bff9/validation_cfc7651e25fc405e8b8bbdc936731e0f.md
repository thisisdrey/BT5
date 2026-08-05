## Analog Found: `CurrentEra`/`ActiveEra` index mismatch traps pool member funds in `pallet-nomination-pools`

### Title
CurrentEra vs. ActiveEra mismatch in `unbond`/`withdraw_unbonded` can dissolve pool points before the underlying stake is actually unlocked, permanently trapping member funds — ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The Plaza Finance bug (H-3) is caused by a function reading a "current period" counter *after* it has been incremented for the next cycle, so the value used to key into the `auctions` mapping no longer matches the key that was actually stored, causing the resolving lookup to return the wrong (empty) slot. The exact same broken-invariant shape exists in `pallet-nomination-pools`: the code captures `T::StakeAdapter::current_era()` and stores it in a variable literally named `active_era`, then uses it both to bucket pool funds (`SubPools::with_era`, `PoolMember::unbonding_eras`) and to decide which of those buckets are mature enough to withdraw — while the real unlocking maturity check performed by the underlying staking pallet is driven by the separate `ActiveEra` storage item, which lags behind `CurrentEra`.

### Finding Description
In `Pallet::<T>::unbond`: [1](#0-0) 
the pool pallet computes `unbond_era = bonding_duration + current_era()` and stores the unbonding funds keyed by this era in both `SubPoolsStorage` (`with_era` map) and the member's `unbonding_eras`.

In `Pallet::<T>::withdraw_unbonded`: [2](#0-1) 
the variable is (mis)named `active_era` but is again populated from `T::StakeAdapter::current_era()`, and is used to call `member.withdraw_unlocked(active_era)`, which dissolves the member's points and unbond-pool balances for every `unbonding_era <= active_era` — *before* the actual call to `T::StakeAdapter::withdraw_unbonded(...)` on the underlying staking system, which internally matures unlocking chunks based on the pallet's real `ActiveEra` (not `CurrentEra`).

`CurrentEra` is a "planned" counter that is bumped ahead of `ActiveEra` during election/session-rotation windows (documented and tested in `staking`/`staking-async`, e.g. `trigger_new_era` bumping `CurrentEra` while `ActiveEra` only advances on `start_era`): [3](#0-2) [4](#0-3) 

Because `pallet-nomination-pools` uses `CurrentEra` (an already-advanced value) as the maturity gate for dissolving its own internal accounting (`SubPools`, `unbonding_eras`), while the real balance release gate on the staking side is `ActiveEra`, the pool pallet's `member.withdraw_unlocked(active_era)` can mark points/balance as "released" (removing them from `SubPools.with_era` / `unbonding_eras`, i.e. from any future accounting) even though `T::StakeAdapter::withdraw_unbonded` has not yet actually released those funds on the staking ledger. This exactly mirrors the Plaza bug: a stale/ahead-of-actual index is used to resolve state that is still keyed by the real (lagging) index, causing the accounting and the true unlock state to permanently diverge.

This is not a hypothetical: it has already happened in production and required a one-time governance migration to recover a trapped member's balance: [5](#0-4) 
> "A bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were dissolved but the held funds weren't released."

The migration in `pr_11018` only remediates the single already-affected member; it does not appear to change the root-cause code path in `unbond`/`withdraw_unbonded` shown above, which still sources both bucketing and dissolution decisions from `current_era()`.

### Impact Explanation
This falls squarely in "permanent user-fund … lock" and "runtime bugs that compromise intended behavior" from the impact gate: a normal, unprivileged user calling the public `withdraw_unbonded` extrinsic can have their pool points/`SubPools` balance dissolved from internal accounting while the corresponding stake is not actually released by the staking backend, permanently locking their funds (unless a bespoke governance migration is run per victim, as already happened once).

### Likelihood Explanation
This requires no adversarial input — it can happen during normal operation whenever a member's unbonding era boundary falls within the window where `CurrentEra` has already been bumped by an election/session rotation but `ActiveEra` has not yet caught up (a documented normal occurrence in both `staking` and `staking-async`). Sherlock's analog bug ("can happen in normal operation") applies directly; the historical `pr_11018` incident confirms this has already occurred at least once in production.

### Recommendation
Use the pallet's real, matured `ActiveEra` (not `CurrentEra`) consistently for both (a) bucketing unbonding funds when calling `unbond`, and (b) gating `withdraw_unlocked` in `withdraw_unbonded`, so pool-side accounting maturity is aligned 1:1 with the staking backend's actual unlock maturity. If `StakingInterface` does not currently expose an `active_era()` accessor to pools, add one and switch `unbond`/`withdraw_unbonded` to use it instead of `current_era()`.

### Proof of Concept
1. Member calls `unbond`; pool pallet computes `unbond_era = bonding_duration + current_era()` (e.g. era 13) and stores this in `SubPools::with_era` and `member.unbonding_eras`.
2. Before `ActiveEra` reaches 13, a session rotation advances `CurrentEra` again (to 14) ahead of `ActiveEra` (still e.g. 12), as is normal during election/session-rotation windows.
3. Once wall-clock/session progression makes `current_era() >= unbond_era` from the pool's perspective, member calls `withdraw_unbonded`; `active_era` variable (really `current_era()`) is now `>= 13`, so `member.withdraw_unlocked(active_era)` dissolves the era-13 bucket and updates `SubPools`/`unbonding_eras` as if released.
4. `T::StakeAdapter::withdraw_unbonded(...)` is called immediately after, but the underlying staking ledger's unlocking-chunk maturity is still governed by `ActiveEra`, which has not yet reached era 13 relative to when the ledger entry was created — resulting in the pool's internal balance/points already zeroed out for that bucket while the corresponding funds remain locked in the staking ledger, exactly the "trapped balance" scenario remediated ad hoc by `pr_11018`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2290-2291)
```rust
			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2408-2439)
```rust
			let mut member =
				PoolMembers::<T>::get(&member_account).ok_or(Error::<T>::PoolMemberNotFound)?;
			let active_era = T::StakeAdapter::current_era();

			let bonded_pool = BondedPool::<T>::get(member.pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?;
			let mut sub_pools =
				SubPoolsStorage::<T>::get(member.pool_id).ok_or(Error::<T>::SubPoolsNotFound)?;

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

			bonded_pool.ok_to_withdraw_unbonded_with(&caller, &member_account)?;
			let pool_account = bonded_pool.bonded_account();

			// NOTE: must do this after we have done the `ok_to_withdraw_unbonded_other_with` check.
			let withdrawn_points = member.withdraw_unlocked(active_era);
			ensure!(!withdrawn_points.is_empty(), Error::<T>::CannotWithdrawAny);
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L619-633)
```rust
		// Increment or set current era.
		let new_planned_era = CurrentEra::<T>::mutate(|s| {
			*s = Some(s.map(|s| s + 1).unwrap_or(0));
			s.unwrap()
		});
		ErasStartSessionIndex::<T>::insert(&new_planned_era, &start_session_index);

		// Clean old era information.
		if let Some(old_era) = new_planned_era.checked_sub(T::HistoryDepth::get() + 1) {
			Self::clear_era_information(old_era);
		}

		// Set staking information for the new era.
		Self::store_stakers_info(exposures, new_planned_era)
	}
```

**File:** substrate/frame/staking-async/src/session_rotation.rs (L860-875)
```rust
	pub(crate) fn start_era(
		ending_era: ActiveEraInfo,
		starting_session: SessionIndex,
		new_era_start_timestamp: u64,
	) {
		// verify that a new era was planned
		debug_assert!(CurrentEra::<T>::get().unwrap_or(0) == ending_era.index + 1);

		let starting_era = ending_era.index + 1;

		// finalize the ending era.
		Self::end_era(&ending_era, new_era_start_timestamp);

		// start the next era.
		Self::start_era_inc_active_era(new_era_start_timestamp);
		Self::start_era_update_bonded_eras(starting_era, starting_session);
```

**File:** prdoc/stable2512-3/pr_11018.prdoc (L1-13)
```text
title: '[Pool] Claim trapped balance via one-time migration'
doc:
- audience: Runtime User
  description: |-
    One-time migration to recover trapped balance for an affected pool member.
    A bug (CurrentEra vs ActiveEra mismatch) caused one pool member's balance to become trapped: their points were
      dissolved but the held funds weren't released. This migration:
    - Applies any pending slash for the member first
    - Calculates trapped amount by checking actual held balance vs expected balance from points
    - Releases trapped funds if present
crates:
- name: pallet-nomination-pools
  bump: minor
```
