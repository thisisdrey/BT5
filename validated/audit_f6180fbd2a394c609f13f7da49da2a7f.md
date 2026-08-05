Audit Report

## Title
Nomination pool `unbond` can dissolve a member's points while issuing zero unbonding balance due to rounding-to-zero in `point_to_balance` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

## Summary
The `unbond` extrinsic converts `unbonding_points` to balance via `bonded_pool.dissolve(unbonding_points)` (which calls `points_to_balance`/`point_to_balance`), and this conversion can legitimately return `0` for a nonzero `unbonding_points` input due to integer-division rounding down. [1](#0-0)  The `unbond` call flow never checks whether the resulting `unbonding_balance` is zero before mutating the member's points and creating the sub-pool entry, allowing a member to permanently burn active points for zero balance credit. [2](#0-1) 

## Finding Description
In the `unbond` dispatchable, after the `ok_to_unbond_with` permission/minimum-bond check (which only validates permissioned/permissionless call conditions and remaining-balance thresholds, not the unbonded-amount magnitude), the code computes `let unbonding_balance = bonded_pool.dissolve(unbonding_points);` and immediately calls `T::StakeAdapter::unbond(...)` and `sub_pools...issue(unbonding_balance)`. [3](#0-2) 

`point_to_balance` returns `Zero::zero()` explicitly when any of `current_balance`, `current_points`, or `points` is zero, and otherwise performs `(current_balance * points) / current_points`, which rounds down and can yield `0` for a nonzero `points` argument when `current_balance * points < current_points`. [1](#0-0) 

Downstream, `member.try_unbond(unbonding_points, points_unbonded, unbond_era)` unconditionally subtracts the full, real `unbonding_points` from `member.points` regardless of whether `points_unbonded` (derived from `unbonding_balance`) is zero, and inserts/accumulates `points_unbonded` (potentially `0`) into `unbonding_eras`. [4](#0-3)  No guard exists anywhere in this path that rejects `unbonding_balance == 0` before these mutations occur, and the `Unbonded` event is emitted with `balance: 0`, masking the loss instead of reverting. [5](#0-4) 

## Impact Explanation
This matches the "permanent user-fund lock" impact category: a pool member's real, previously-bonded points are irreversibly destroyed in exchange for an unbonding-pool claim worth exactly `0`, with no revert and no recovery path — a subsequent `withdraw_unbonded` will also compute `0` via the same `point_to_balance` rounding on the now-zero-balance sub-pool entry. The loss is silent (masked by a successful `Unbonded` event showing `balance: 0`) and permanent once unbonding is initiated.

## Likelihood Explanation
This is directly reachable by any unprivileged signed account that is a pool member, requiring no governance, validator, or peer collusion — only a public `unbond` transaction with a small `unbonding_points` value chosen relative to a pool where `points` significantly exceeds `balance` (e.g., after slashing), a condition explicitly acknowledged in the module's own documentation about point/balance ratio drift. [6](#0-5)  The exploit is trivially repeatable and requires only normal user capability.

## Recommendation
Add an explicit check in `unbond` that `unbonding_balance` (returned from `bonded_pool.dissolve(unbonding_points)`) is non-zero before proceeding to call `T::StakeAdapter::unbond`, issue sub-pool points, and mutate `member.points`, e.g. via `ensure!(!unbonding_balance.is_zero(), Error::<T>::...)`. Apply the analogous check to any other point/balance dissolution path (e.g., `withdraw_unbonded`) that can similarly round to zero.

## Proof of Concept
1. Drive a bonded pool into a state where `bonded_pool.points` is large relative to the bonded balance (e.g., via slashing), so that `current_balance / current_points` is a small fraction.
2. As a pool member with nonzero points, call `unbond(origin, member_account, unbonding_points)` with `unbonding_points` small enough that `(current_balance * unbonding_points) / current_points == 0` in `point_to_balance`.
3. Observe the call succeeds: `member.points` decreases by `unbonding_points`, `Event::Unbonded { balance: 0, .. }` is emitted, and the unbonding sub-pool entry holds `0` points for that era.
4. After the bonding duration, call `withdraw_unbonded` — payout is `0`, confirming permanent loss of the unbonded points' value.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L197-203)
```rust
//!   either the bonded pool or any of the unbonding pools. The crucial fact is that in any of these
//!   pools, the ratio of point to balance is different and might not be 1. Each pool starts with a
//!   ratio of 1, but as time goes on, for reasons such as slashing, the ratio gets broken. Over
//!   time, 100 points in a bonded pool can be worth 90 DOTs. Make sure you are either representing
//!   points as points (not as DOTs), or even better, always display both: “You have x points in
//!   pool y which is worth z DOTs”. See here and here for examples of how to calculate point to
//!   balance ratio of each pool (it is almost trivial ;))
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L633-659)
```rust
	fn try_unbond(
		&mut self,
		points_dissolved: BalanceOf<T>,
		points_issued: BalanceOf<T>,
		unbonding_era: EraIndex,
	) -> Result<(), Error<T>> {
		if let Some(new_points) = self.points.checked_sub(&points_dissolved) {
			match self.unbonding_eras.get_mut(&unbonding_era) {
				Some(already_unbonding_points) => {
					*already_unbonding_points =
						already_unbonding_points.saturating_add(points_issued)
				},
				None => self
					.unbonding_eras
					.try_insert(unbonding_era, points_issued)
					.map(|old| {
						if old.is_some() {
							defensive!("value checked to not exist in the map; qed");
						}
					})
					.map_err(|_| Error::<T>::MaxUnbondingLimit)?,
			}
			self.points = new_points;
			Ok(())
		} else {
			Err(Error::<T>::MinimumBondNotMet)
		}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2257-2323)
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2325-2331)
```rust
			Self::deposit_event(Event::<T>::Unbonded {
				member: member_account.clone(),
				pool_id: member.pool_id,
				points: points_unbonded,
				balance: unbonding_balance,
				era: unbond_era,
			});
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
