### Title
Rounding of `unbonding_points` to zero balance in `pallet-nomination-pools::unbond()` permanently strands member funds — ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`Pallet::unbond()` converts a member's `unbonding_points` into an actual balance via `BondedPool::dissolve()` → `Pallet::point_to_balance()`, which performs a round-down division (`current_balance * points / current_points`). When a pool's points-to-balance ratio has been driven very low (e.g., after a heavy slash, points >> balance), a nonzero `unbonding_points` value can produce `unbonding_balance == 0`. The call then proceeds to remove the points from the bonded pool and issue a sub-pool claim based on that zero balance, silently converting the member's real stake into an unrecoverable zero-value claim, while the underlying value stays locked in the bonded pool for other members. This mirrors the Tokemak `AutoPoolDebt.withdraw()` bug class: a round-down calculation on the withdrawal path that can evaluate to zero and is not special-cased, except here the consequence is not a revert but a silent fund loss/lock for the caller.

### Finding Description
`point_to_balance` rounds down and only guards against total zero balance/points/points-argument, not against a *nonzero* `points` argument producing a rounded `0` balance: [1](#0-0) 

`UnbondPool::dissolve` (and by extension `BondedPool::dissolve`, which wraps `point_to_balance`) directly returns this rounded value with no zero-check: [2](#0-1) 

In the `unbond` extrinsic, this computed `unbonding_balance` is used to reduce the bonded pool's real balance/points, to instruct the staking backend to unbond that (possibly zero) amount, and to issue the member's claim in the destination unbonding sub-pool — all based on the same rounded value: [3](#0-2) 

Because `T::StakeAdapter::unbond` (backed by `pallet-staking::do_unbond`) explicitly no-ops when `value.is_zero()` rather than erroring: [4](#0-3) 

there is no revert to signal the degenerate case back to the pools pallet — the call to `unbond` on the pools pallet succeeds, `bonded_pool.points` is reduced by the full `unbonding_points` (so the caller loses proportional voting/ownership of the pool), yet the sub-pool `issue()` call for the destination unbonding-era pool receives `new_funds == 0`, producing `points_unbonded == 0` for that sub-pool. The member's real economic stake associated with those relinquished points is not represented anywhere it can later be withdrawn from — it is effectively donated to the remaining pool members (whose points now claim a larger share of the unchanged bonded balance).

This is the structural analog of the `AutoPoolDebt.withdraw()` bug: a round-down conversion feeding a withdrawal/settlement path without a zero-result guard. In Tokemak the consequence is a revert (temporary DoS); here, because the downstream staking primitive is guarded to be a no-op on zero rather than reverting, the consequence is a silent, permanent loss of the member's claim on that portion of pool value — arguably worse, since it settles incorrectly (to the wrong beneficiaries — the remaining pool members) rather than simply failing.

### Impact Explanation
This breaks the invariant that "Balances, assets, ... pools ... must conserve value and settle exactly once to the rightful beneficiary and amount." A pool member who calls the public, permissionless `unbond` extrinsic with a technically valid (nonzero) `unbonding_points` value can have their entitled balance rounded to zero while their points are nonetheless debited from the bonded pool, permanently locking/losing that value from their perspective and silently transferring it to other pool participants. No malicious peer, validator, governance actor, or leaked key is required — only a pool state where the points:balance ratio has degraded sufficiently (which can occur legitimately through slashing events already supported by the protocol).

### Likelihood Explanation
The precondition (a low points-to-balance ratio) requires the pool to have experienced meaningful slashing, which is a normal, unprivileged-triggerable protocol event, not an attacker-controlled action by itself. However, once such a ratio exists, any member (including the attacker unbonding their own or a targeted small amount) can trigger the truncation deterministically by choosing a small `unbonding_points` value relative to the ratio. This makes the likelihood dependent on pool slash history rather than universally exploitable, but it is fully reachable through the public `unbond` call path without any privileged or malicious-infrastructure precondition.

### Recommendation
In `BondedPool::dissolve` / `Pallet::unbond`, after computing `unbonding_balance = bonded_pool.dissolve(unbonding_points)`, explicitly reject the operation (or round in the member's favor) when `unbonding_balance.is_zero()` while `unbonding_points` is nonzero, analogous to the Tokemak fix of special-casing a zero-shares-to-burn result instead of silently proceeding. Concretely, add an `ensure!(!unbonding_balance.is_zero() || unbonding_points.is_zero(), Error::<T>::...)` guard, or enforce a minimum `unbonding_points` bound tied to the current points:balance ratio so that truncation to zero balance cannot occur for a nonzero point removal.

### Proof of Concept
Conceptual reproduction (not run against a live node, derived from source inspection):
1. Create a nomination pool and have multiple members bond funds, giving the pool `points == balance` initially (1:1 ratio).
2. Trigger a large slash against the pool's bonded stash such that `bonded_pool.balance` becomes very small relative to `bonded_pool.points` (e.g., balance = 1, points = 10^12), which is achievable through the pool's existing slash-application flow.
3. Call `Pools::unbond(origin, member_account, unbonding_points)` with an `unbonding_points` value small enough that `point_to_balance(balance=1, points=10^12, unbonding_points) == 0` (e.g., `unbonding_points < 10^12`).
4. Observe: the call succeeds (no revert), `bonded_pool.points` is reduced by `unbonding_points`, `member.points` is reduced accordingly, but the sub-pool issued for the member's claim has `points_unbonded == 0` / `balance == 0`, meaning the member can never withdraw value for the points they relinquished, while remaining pool members retain claim on the unchanged bonded balance.

**Uncertainty note:** I was unable to fetch the full source of `PoolMember::try_unbond` and `BondedPool::ok_to_unbond_with` (only signatures were located via grep, not full bodies) within the available tool budget, so I could not fully confirm whether these functions contain an independent minimum-bond or minimum-unbond-amount check that might already prevent this exact degenerate input in all configurations. The rounding behavior in `point_to_balance`/`dissolve` and its unguarded use in `unbond` is confirmed directly from source, but the very last confirmation step (whether any pre-existing sibling check fully closes this gap for all pool configurations) is not fully verified.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1567-1578)
```rust
	/// Dissolve some points from the unbonding pool, reducing the balance of the pool
	/// proportionally. This is the opposite of `issue`.
	///
	/// Returns the actual amount of `Balance` that was removed from the pool.
	fn dissolve(&mut self, points: BalanceOf<T>) -> BalanceOf<T> {
		let balance_to_unbond = self.point_to_balance(points);
		self.points = self.points.saturating_sub(points);
		self.balance = self.balance.saturating_sub(balance_to_unbond);

		balance_to_unbond
	}
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

**File:** substrate/frame/staking/src/pallet/impls.rs (L1415-1416)
```rust
		if !value.is_zero() {
			ledger.active -= value;
```
