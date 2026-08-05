All code paths cited in the claim match the current repository exactly, confirming the vulnerability is real and reachable.

Audit Report

## Title
`unbond` burns member points while dissolving zero underlying balance on heavily-slashed pools - ([File: substrate/frame/nomination-pools/src/lib.rs])

## Summary
`Pallet::unbond` calls `bonded_pool.dissolve(unbonding_points)`, which computes `balance = self.points_to_balance(points)` before decrementing `self.points` unconditionally, and `points_to_balance` performs an integer division `(current_balance * points) / current_points` that truncates to zero when a pool's points-to-balance ratio is below 1 (post heavy-slash) and `points` is small. [1](#0-0) [2](#0-1)  The resulting zero `unbonding_balance` is nonetheless passed to `T::StakeAdapter::unbond`, issued into the era's `UnbondPool`, and the member's real `active_points` are decremented by the full `unbonding_points` amount via `member.try_unbond`. [3](#0-2)  This permanently destroys the caller's claim on pool value with no balance received in return.

## Finding Description
The `ok_to_unbond_with` guard only checks the *remaining* member balance after unbonding against `MinJoinBond`/`depositor_min_bond`, or permits a full unbond — it never validates that the *dissolved* balance itself (`points_to_balance(unbonding_points)`) is non-zero. [4](#0-3)  Consequently, when a pool has been slashed such that `points > balance` (ratio below 1), a member can select a small `unbonding_points` value that satisfies the remaining-balance check but truncates to `0` in `point_to_balance`'s integer division. [5](#0-4)  The `dissolve` function computes this zero balance, still subtracts the full `points` from `self.points`, and returns `0` unconditionally — there is no `ensure!(!balance.is_zero(), ...)` guard analogous to `pallet-asset-conversion::do_remove_liquidity`'s zero-amount checks. [1](#0-0)  The `unbond` extrinsic then unconditionally issues the (zero) balance into the unbonding sub-pool and reduces the member's real points by the full requested amount. [6](#0-5) 

## Impact Explanation
This is a value-conservation violation in the nomination-pools staking layer: a member permanently loses claim on stake-pool points/value while receiving zero balance credited to the unbonding sub-pool or released from the underlying staking adapter. It matches the required pivot criterion that staking and pools logic must conserve value and settle exactly once — here it settles for zero while still burning the member's real points, an uncompensated loss of user funds. [7](#0-6) 

## Likelihood Explanation
This requires: (1) a pool that has suffered a slash severe enough to push balance below points (ratio < 1), and (2) any unprivileged member calling the public `unbond` extrinsic with a `points` amount small enough for the integer division to truncate to zero, while still satisfying `ok_to_unbond_with`'s remaining-balance check. Both conditions are reachable through normal, unprivileged usage of public extrinsics — no malicious validator, relayer, or governance action is required — making this feasible and repeatable in any sufficiently slashed pool with many small stakers.

## Recommendation
Add an explicit guard rejecting unbonds whose resulting dissolved balance is zero while `unbonding_points` is non-zero — e.g., `ensure!(!bonded_pool.points_to_balance(unbonding_points).is_zero(), Error::<T>::AmountTooSmallToUnbond)` inside `ok_to_unbond_with` or immediately before calling `dissolve` in the `unbond` extrinsic — mirroring the non-zero-amount checks already used in `pallet-asset-conversion::do_remove_liquidity`.

## Proof of Concept
1. Create a pool and have members join such that `bonded_pool.points` is large relative to the bonded balance.
2. Trigger a slash via the staking adapter large enough to drop `balance/points` below 1 (e.g., balance=100, points=1000).
3. Call `Pools::unbond(origin, member_account, small_points)` where `small_points` is chosen such that `(balance * small_points) / total_points == 0` via integer truncation, while still satisfying the `ok_to_unbond_with` remaining-balance check.
4. Observe the call succeeds, `member.points` decreases by `small_points`, `Event::Unbonded { balance: 0, .. }` is emitted, and no real value is moved into the unbonding sub-pool or released from the underlying stake adapter — the member irrecoverably loses `small_points` worth of claim for zero balance in return.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1093-1099)
```rust
	fn dissolve(&mut self, points: BalanceOf<T>) -> BalanceOf<T> {
		// NOTE: do not optimize by removing `balance`. it must be computed before mutating
		// `self.point`.
		let balance = self.points_to_balance(points);
		self.points = self.points.saturating_sub(points);
		balance
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1217-1252)
```rust
	fn ok_to_unbond_with(
		&self,
		caller: &T::AccountId,
		target_account: &T::AccountId,
		target_member: &PoolMember<T>,
		unbonding_points: BalanceOf<T>,
	) -> Result<(), DispatchError> {
		let is_permissioned = caller == target_account;
		let is_depositor = *target_account == self.roles.depositor;
		let is_full_unbond = unbonding_points == target_member.active_points();

		let balance_after_unbond = {
			let new_depositor_points =
				target_member.active_points().saturating_sub(unbonding_points);
			let mut target_member_after_unbond = (*target_member).clone();
			target_member_after_unbond.points = new_depositor_points;
			target_member_after_unbond.active_balance()
		};

		// any partial unbonding is only ever allowed if this unbond is permissioned.
		ensure!(
			is_permissioned || is_full_unbond,
			Error::<T>::PartialUnbondNotAllowedPermissionlessly
		);

		// any unbond must comply with the balance condition:
		ensure!(
			is_full_unbond ||
				balance_after_unbond >=
					if is_depositor {
						Pallet::<T>::depositor_min_bond()
					} else {
						MinJoinBond::<T>::get()
					},
			Error::<T>::MinimumBondNotMet
		);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2290-2323)
```rust
			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

			// Note that we lazily create the unbonding pools here if they don't already exist
			let mut sub_pools = SubPoolsStorage::<T>::get(member.pool_id)
				.unwrap_or_default()
				.maybe_merge_pools(active_era);

			// Update the unbond pool associated with the current era with the unbonded funds. Note
			// that we lazily create the unbond pool if it does not yet exist.
			if !sub_pools.with_era.contains_key(&unbond_era) {
				sub_pools
					.with_era
					.try_insert(unbond_era, UnbondPool::default())
					// The above call to `maybe_merge_pools` should ensure there is
					// always enough space to insert.
					.defensive_map_err::<Error<T>, _>(|_| {
						DefensiveError::NotEnoughSpaceInUnbondPool.into()
					})?;
			}

			let points_unbonded = sub_pools
				.with_era
				.get_mut(&unbond_era)
				// The above check ensures the pool exists.
				.defensive_ok_or::<Error<T>>(DefensiveError::PoolNotFound.into())?
				.issue(unbonding_balance);

			// Try and unbond in the member map.
			member.try_unbond(unbonding_points, points_unbonded, unbond_era)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3503-3522)
```rust
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
