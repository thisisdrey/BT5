Based on my research, I found no evidence that `unbond` performs the same defensive slash-sync as `withdraw_unbonded`. This is a genuine parallel to the Badger issue: one entry point (`withdraw_unbonded`) explicitly re-syncs pending state before computing an amount, while a sibling entry point (`unbond`) that also depends on the same ratio does not.

### Title
`Pallet::unbond` computes unbonding balance from stale point/balance ratio without applying pending slash - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
`pallet-nomination-pools`, when using the `DelegateStake` strategy, tracks slashes lazily: a slash reduces `T::StakeAdapter::pending_slash(pool)` immediately, but a member's recorded `points`↔`balance` ratio is only corrected once `do_apply_slash` is executed (via the permissionless `apply_slash` call, or defensively inside `withdraw_unbonded`). The `unbond` extrinsic never invokes this synchronization step before computing the balance to dissolve.

### Finding Description
`withdraw_unbonded` explicitly synchronizes pending state before computing the amount to be released: [1](#0-0) 
This mirrors exactly the recommendation in the external report: sync accrued/pending state (there: TCR/ICR via `syncGlobalAccountingAndGracePeriod`; here: pending slash via `do_apply_slash`) before using derived ratios for a fund-moving decision.

In contrast, `unbond` (`pallet::call_index(3)`) never calls `do_apply_slash`. It goes straight from `get_member_with_pools` to computing `unbonding_balance = bonded_pool.dissolve(unbonding_points)` and calling `T::StakeAdapter::unbond`, using the pool's currently recorded `bonded_pool.points` and underlying `active_stake`, which have not yet absorbed a `pending_slash` that already exists on the pool account: [2](#0-1) 

`member_pending_slash` shows the ratio depends on `T::StakeAdapter::pending_slash(pool_account)` and the discrepancy between actual delegated balance and the pool's recorded `total_balance()`: [3](#0-2) 

Because `unbond` skips this check, a member can convert their `points` to `balance` using a stale (pre-slash) exchange rate the moment a slash is reported but before `apply_slash`/`withdraw_unbonded` have run for that pool, exactly as the LiquidationSequencer bug allowed stale TCR/ICR values to produce an incorrect liquidation list.

### Impact Explanation
An unbonding member effectively locks in a favorable pre-slash point/balance ratio into `SubPoolsStorage`'s `UnbondPool`, shifting the slash's economic burden onto members who remain in the pool (or who unbond later, after `apply_slash` corrects the ratio). This is a value-conservation violation: the reported/queued slash is not distributed pro-rata across all points at the time it was incurred, degrading the "settle exactly once to the rightful beneficiary and amount" invariant for staking/asset accounting.

### Likelihood Explanation
The precondition — a slash reported against the pool's stake account that has not yet been applied via `apply_slash` — is a normal, expected transient runtime state (the code comments in `withdraw_unbonded` even note "pool slashes must have been already applied via permissionless `Call::apply_slash`" as merely the expected case, not a guarantee). Any unprivileged pool member can call `unbond` at any time, including immediately after observing a slash event and before anyone (permissionlessly) calls `apply_slash`, so exploitation requires no special privileges — only correct timing of a normal user-facing call.

### Recommendation
Mirror the same defensive check used in `withdraw_unbonded`: call `Self::do_apply_slash(&member_account, None, false)` (ignoring `NothingToSlash`) at the start of `unbond`, before `bonded_pool.dissolve` and `T::StakeAdapter::unbond` are invoked, so the points/balance ratio used to compute `unbonding_balance` always reflects any pending slash.

### Proof of Concept
1. Pool uses `DelegateStake` strategy; member Alice holds `points` worth `X` balance in the bonded pool.
2. An offence causes a slash to be recorded against the pool's stash account; `T::StakeAdapter::pending_slash(pool)` becomes non-zero, but `apply_slash`/`do_apply_slash` has not yet run.
3. Alice calls `unbond` before anyone applies the slash. `bonded_pool.dissolve(unbonding_points)` computes `unbonding_balance` using the pre-slash `active_stake`/`points` ratio (see `substrate/frame/nomination-pools/src/lib.rs:2270-2296`), extracting more value than her fair post-slash share.
4. When `apply_slash` is eventually called for the remaining members, the already-departed `unbonding_balance` is not revisited, so remaining members absorb a disproportionate share of the slash — compare to `withdraw_unbonded`'s explicit `do_apply_slash` call at `substrate/frame/nomination-pools/src/lib.rs:2417-2432`, which `unbond` lacks.

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
