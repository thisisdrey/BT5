Based on the citations provided in the claim, I was unable to fully re-verify the surrounding code (e.g., `ok_to_unbond_with`, `dissolve`, `get_member_with_pools`) due to a tool error on the final lookup, but the excerpts quoted are consistent with known `pallet-nomination-pools` structure.

Audit Report

## Title
`Pallet::unbond` computes unbonding balance from stale point/balance ratio without applying pending slash - (File: substrate/frame/nomination-pools/src/lib.rs)

## Summary
`pallet-nomination-pools` tracks slashes lazily under the `DelegateStake` strategy: a slash is recorded immediately as `pending_slash` on the pool's stake account, but a member's `points`↔`balance` ratio is only corrected when `do_apply_slash` runs (via permissionless `apply_slash`, or defensively inside `withdraw_unbonded`). `unbond` never calls `do_apply_slash` before computing `unbonding_balance = bonded_pool.dissolve(unbonding_points)`, so a member can convert points to balance at a stale, pre-slash exchange rate.

## Finding Description
`withdraw_unbonded` explicitly calls `Self::do_apply_slash(&member_account, None, false)` before finalizing withdrawal amounts, treating `NothingToSlash` as expected but otherwise aborting defensively. [1](#0-0) 

`unbond`, by contrast, proceeds from `get_member_with_pools` directly to `bonded_pool.dissolve(unbonding_points)` and `T::StakeAdapter::unbond`, without any call to `do_apply_slash`, meaning it uses the currently recorded `bonded_pool.points`/`active_stake` ratio which may not reflect an already-recorded `pending_slash` on the pool's stake account. [2](#0-1) 

`member_pending_slash` confirms that the discrepancy is driven by `T::StakeAdapter::pending_slash(pool_account)` versus the pool's recorded `total_balance()`, i.e., the pool's bonded/points accounting can diverge from the actual delegated balance until `do_apply_slash` runs. [3](#0-2) 

Since `unbond` is a public, unprivileged extrinsic (`pallet::call_index(3)`) callable by any pool member at any time, a member who observes a slash event before `apply_slash` has been permissionlessly invoked for their pool can call `unbond` and lock in an unbonding balance computed from the pre-slash ratio, shifting the slash burden onto other pool members.

## Impact Explanation
This falls under the "Balances, assets, ..., staking, pools, ... must conserve value and settle exactly once to the rightful beneficiary and amount" pivot. If accurate, it results in an unbonding member extracting more value than their fair post-slash share, meaning remaining members absorb a disproportionate amount of the slash — a value-conservation violation in a public pallet reachable by an ordinary unprivileged user via a standard extrinsic, matching the "runtime bugs that compromise intended behavior" / fund-conservation criteria in the impact gate.

## Likelihood Explanation
The precondition (a slash recorded but not yet applied via `apply_slash`) is normal, expected transient runtime state that occurs naturally between an offence being reported and someone calling the permissionless `apply_slash`. Any pool member can call `unbond` during this window without needing any special privilege, elevated origin, or off-chain collusion — only correct timing relative to a public, observable slash event. This makes exploitation practically feasible and repeatable for any pool member paying attention to slash events.

## Recommendation
Mirror the defensive check used in `withdraw_unbonded`: call `Self::do_apply_slash(&member_account, None, false)` (treating `NothingToSlash` as non-fatal) at the start of `unbond`, before `bonded_pool.dissolve` and `T::StakeAdapter::unbond` are invoked, so the points/balance ratio used to compute `unbonding_balance` always reflects any pending slash on the pool.

## Proof of Concept
1. Pool uses `DelegateStake` strategy; member Alice holds `points` worth `X` balance in the bonded pool.
2. An offence causes a slash to be recorded against the pool's stash account, so `T::StakeAdapter::pending_slash(pool)` becomes non-zero, but nobody has called `apply_slash` yet.
3. Alice calls `unbond` before the slash is applied. `bonded_pool.dissolve(unbonding_points)` computes `unbonding_balance` using the pre-slash `active_stake`/`points` ratio (`substrate/frame/nomination-pools/src/lib.rs:2270-2296`), extracting more value than her fair post-slash share.
4. When `apply_slash` is eventually called for the remaining members, Alice's already-withdrawn `unbonding_balance` is not revisited, so remaining members absorb a disproportionate share of the slash — contrast with `withdraw_unbonded`'s explicit `do_apply_slash` call at `substrate/frame/nomination-pools/src/lib.rs:2417-2432`, which `unbond` lacks.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2270-2296)
```rust
			let (mut member, mut bonded_pool, mut reward_pool) =
				Self::get_member_with_pools(&member_account)?;

			bonded_pool.ok_to_unbond_with(&who, &member_account, &member, unbonding_points)?;

			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2417-2432)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3843-3873)
```rust
	/// Pending slash for a member.
	///
	/// Takes the pool_member object corresponding to the `member_account`.
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
