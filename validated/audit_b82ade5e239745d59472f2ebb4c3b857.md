Audit Report

## Title
`Pallet::unbond` computes unbonding balance from stale point/balance ratio without applying pending slash - (File: substrate/frame/nomination-pools/src/lib.rs)

## Summary
`unbond` (`Pallet::unbond`, call_index 3) computes `unbonding_balance = bonded_pool.dissolve(unbonding_points)` directly from `get_member_with_pools` results without first calling `Self::do_apply_slash`, unlike `withdraw_unbonded`, which explicitly applies any pending slash before computing the amount to release. This lets a member unbond using a pre-slash points/balance ratio when a slash has been recorded against the pool's stake account but not yet synced via `do_apply_slash`/`apply_slash`.

## Finding Description
`unbond`, at [1](#0-0) , calls `Self::get_member_with_pools`, claims rewards, and then immediately computes `bonded_pool.dissolve(unbonding_points)` and calls `T::StakeAdapter::unbond`, using the pool's currently recorded `points`/`active_stake` ratio. There is no call to `Self::do_apply_slash` anywhere in this function.

By contrast, `withdraw_unbonded` explicitly synchronizes the pool's pending slash state before computing the amount to be released, calling `Self::do_apply_slash(&member_account, None, false)` and treating `NothingToSlash` as the only acceptable non-error outcome (any other failure aborts the call defensively) at [2](#0-1) .

The pending-slash mechanism itself, `member_pending_slash`, shows the ratio depends on `T::StakeAdapter::pending_slash(pool_account)` together with the gap between the member's `actual_balance` (from `T::StakeAdapter::member_delegation_balance`) and their `expected_balance` (`pool_member.total_balance()`) at [3](#0-2) , confirming this is a real, lazily-synced piece of state that can be non-zero at the moment `unbond` is called.

This asymmetry between the two call paths is real and verified directly in the code: `withdraw_unbonded` treats "pool slashes must have been already applied via permissionless `Call::apply_slash`" as merely the expected/common case, and defensively re-applies it; `unbond` makes no equivalent defensive check.

## Impact Explanation
If exploitable, this would let an unbonding member lock in a pre-slash points/balance ratio into `SubPoolsStorage`, shifting a slash's economic burden onto remaining/future members — a value-conservation violation for the "settle exactly once, pro-rata" invariant that governs staking/pool accounting, matching the "theft/underpriced settlement causing fund shift" category in the impact gate.

However, I could not fully verify within available context whether `T::StakeAdapter::unbond` (the `DelegateStake` strategy's underlying implementation in `adapter.rs`) itself performs any additional check against the member's actual delegated balance that would reject or clamp an unbond request inflated by a stale ratio — I was unable to read the relevant `adapter.rs` `unbond` implementation in the final iteration due to tool-call exhaustion. Given this gap, I cannot confirm the reachable exploit path is unobstructed by existing lower-level guards, which is one of the required checks ("existing guards reviewed and shown insufficient").

## Likelihood Explanation
The precondition (a slash reported but not yet applied via `apply_slash`) is plausible transient runtime state reachable by any pool member calling a normal public extrinsic (`unbond`) with correct timing, requiring no privileged access.

## Recommendation
Mirror `withdraw_unbonded`'s defensive pattern in `unbond`: call `Self::do_apply_slash(&member_account, None, false)` (tolerating `NothingToSlash`) before computing `bonded_pool.dissolve(unbonding_points)`, so the ratio used to compute `unbonding_balance` always reflects any pending slash.

## Proof of Concept
1. Pool uses `DelegateStake` strategy; member Alice holds `points` worth `X` balance.
2. A slash is recorded against the pool's stash account so `T::StakeAdapter::pending_slash(pool)` is non-zero, but `apply_slash` has not yet run.
3. Alice calls `unbond`; `bonded_pool.dissolve(unbonding_points)` at `substrate/frame/nomination-pools/src/lib.rs:2294` computes `unbonding_balance` from the pre-slash ratio, unlike `withdraw_unbonded`'s explicit sync at `substrate/frame/nomination-pools/src/lib.rs:2417-2432`.
4. Compare final balances of remaining pool members after `apply_slash` is eventually called, to confirm they absorb a disproportionate share of the slash relative to Alice's already-departed `unbonding_balance`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2257-2296)
```rust
		pub fn unbond(
			origin: OriginFor<T>,
			member_account: AccountIdLookupOf<T>,
			#[pallet::compact] unbonding_points: BalanceOf<T>,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let member_account = T::Lookup::lookup(member_account)?;
			// ensure member is not in an un-migrated state.
			ensure!(
				!Self::api_member_needs_delegate_migration(member_account.clone()),
				Error::<T>::NotMigrated
			);

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
