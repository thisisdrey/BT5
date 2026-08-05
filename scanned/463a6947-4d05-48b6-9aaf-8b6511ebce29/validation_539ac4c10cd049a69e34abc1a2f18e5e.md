### Title
Nomination pool `unbond` can dissolve a member's points while issuing zero unbonding balance due to rounding-to-zero in `point_to_balance` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`pallet-nomination-pools`'s `unbond` extrinsic converts `unbonding_points` to an underlying balance via `Pallet::<T>::point_to_balance`, which performs integer division and can legitimately return `0` when `current_balance * points < current_points` [1](#0-0)  . The `unbond` call flow never checks that the resulting `unbonding_balance` is non-zero before mutating the member's permanent point balance and creating the corresponding sub-pool entry, so a member can permanently lose bonded points while being credited with zero claim on the pool — exactly the "burn shares, receive nothing" pattern from the external Notional report.

### Finding Description
In `unbond`, the member's claim on the bonded pool is computed by `bonded_pool.dissolve(unbonding_points)`, which internally calls `points_to_balance`/`point_to_balance` [2](#0-1) . That helper explicitly returns `Zero::zero()` for the resulting balance when `current_balance.is_zero() || current_points.is_zero() || points.is_zero()`, and otherwise performs `(current_balance * points) / current_points`, which rounds down and can produce `0` for any set of positive inputs where the product is smaller than `current_points` [1](#0-0) .

The `unbond` dispatchable does not guard against this zero result:

```
let unbonding_balance = bonded_pool.dissolve(unbonding_points);
T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;
...
let points_unbonded = sub_pools.with_era.get_mut(&unbond_era)...issue(unbonding_balance);
member.try_unbond(unbonding_points, points_unbonded, unbond_era)?;
``` [3](#0-2) 

If `unbonding_balance == 0`, `sub_pools...issue(0)` returns `0` new points for the unbonding sub-pool, and `member.try_unbond(unbonding_points, 0, unbond_era)` still succeeds: it unconditionally subtracts `points_dissolved` (the real, non-zero `unbonding_points` requested by the user) from `member.points` and inserts/accumulates `0` into `unbonding_eras` for that era [4](#0-3) . The member has thus permanently burned active points in the bonded pool but obtained an unbonding-pool position worth `0` balance — when they later call `withdraw_unbonded`, `point_to_balance` on the `UnbondPool` with `0` points will again return `0`, so they receive nothing back for the tokens they gave up.

`ok_to_unbond_with` is only intended to enforce minimum-bond thresholds for the *remaining* balance of privileged callers; it is not designed to reject the specific case where the *unbonded* amount itself rounds to zero. Since `unbonding_points` is fully attacker/user-controlled, a member — particularly in pools with a large `points`/`balance` ratio (e.g., after historical slashes or dust manipulation) — can trigger this by choosing a small enough `unbonding_points` value. No malicious peer, validator, admin, or governance action is needed; it is a straightforward single-account interaction with a public dispatchable.

### Impact Explanation
This falls under "theft or unbacked mint or unlock" / "permanent user-fund lock" impact category: a pool member loses real, previously-locked stake represented by points, in exchange for an unbonding claim of exactly zero, with no ability to reclaim it. Because unbonding is generally irreversible once initiated, the loss is permanent for that fraction of points. Repeated small unbonds by an attacker against their own account, or accidental use by unaware users on pools with skewed points/balance ratios, causes silent fund loss with no revert or event to signal failure (the `Unbonded` event is even emitted with `balance: 0`, effectively masking the loss) [5](#0-4) .

### Likelihood Explanation
The rounding condition is a normal, expected outcome of the points/balance accounting design (documented in the module-level docs describing that "100 points in a bonded pool can be worth 90 DOTs" and value per point is not intrinsically 1) [6](#0-5) . Any pool that has experienced slashing, or where `points` significantly exceeds `balance`, creates a window where small `unbonding_points` values map to `0` balance. This requires only a normal signed transaction from an existing pool member — no privileged role, collusion, or infrastructure compromise — making it directly reachable by any unprivileged user.

### Recommendation
In the `unbond` extrinsic (and analogously in `withdraw_unbonded`'s point/balance dissolution), require that the computed `unbonding_balance` (and later `balance_to_unbond`) be strictly greater than zero before mutating `member.points` / `bonded_pool.points`, e.g.:
```rust
let unbonding_balance = bonded_pool.dissolve(unbonding_points);
ensure!(!unbonding_balance.is_zero(), Error::<T>::???); // new "ZeroBalanceToUnbond" style error, or fold into existing MinimumBondNotMet
```
Alternatively, bound the minimum `unbonding_points` a caller may specify relative to the pool's current points-to-balance ratio so that the resulting `unbonding_balance` cannot round to zero, consistent with how ERC4626-style vaults revert on zero-share/zero-asset conversions.

### Proof of Concept
1. Create a bonded pool and drive it to a state where `bonded_pool.points` is much larger than `bonded_pool` bonded balance (e.g., via slashing or by depositing at a highly skewed points ratio, matching the documented "100 points : 90 DOTs" scenario).
2. As a pool member holding a nonzero but small number of points, call `unbond(origin, member_account, unbonding_points)` with an `unbonding_points` value small enough that `(current_balance * unbonding_points) / current_points == 0` per `point_to_balance` [7](#0-6) .
3. Observe: the call succeeds, `member.points` is reduced by `unbonding_points`, `Event::Unbonded { ..., balance: 0, ... }` is emitted, and the member's new unbonding-era entry holds `0` points in the sub-pool.
4. After the unbonding period, call `withdraw_unbonded` for this member: the payout is `0`, confirming the member permanently lost `unbonding_points` worth of pool ownership for no compensation.

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1072-1078)
```rust
	/// Convert the given number of points to balance given the current pool state.
	///
	/// This is often used for unbonding.
	fn points_to_balance(&self, points: BalanceOf<T>) -> BalanceOf<T> {
		let bonded_balance = T::StakeAdapter::active_stake(Pool::from(self.bonded_account()));
		Pallet::<T>::point_to_balance(bonded_balance, self.points, points)
	}
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
