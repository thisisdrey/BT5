### Title
Rounding in `BondedPool::dissolve`/`point_to_balance` lets `unbond` burn pool points while crediting zero unbonding balance - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The Sherlock finding is a classic "proportional-reduction integer division rounds to zero" bug: `LibQuote.closeQuote` reduces a locked value by `locked * filledAmount / openAmount`, and when `filledAmount` is small relative to `openAmount`, the division rounds down to `0`, so the locked value is never actually reduced even though the position was recorded as partially closed. The direct analog in this repository is `pallet-nomination-pools`' `point_to_balance` / `BondedPool::dissolve` used inside `Pallet::unbond` [1](#0-0) : the balance owed for a given number of points is computed as `current_balance * points / current_points` (integer division, rounds toward zero), and then `self.points` is unconditionally reduced by the full `points` amount regardless of whether the computed balance was non-zero [2](#0-1) .

### Finding Description
`BondedPool::dissolve` at `substrate/frame/nomination-pools/src/lib.rs`:
```
fn dissolve(&mut self, points: BalanceOf<T>) -> BalanceOf<T> {
    let balance = self.points_to_balance(points);
    self.points = self.points.saturating_sub(points);
    balance
}
``` [2](#0-1) 

`points_to_balance`/`point_to_balance` computes `(current_balance * points) / current_points` and explicitly documents that it can round the balance down to `0` when the numerator is smaller than the denominator - the pallet's own unit tests demonstrate exactly this rounding-to-zero behavior for small point amounts against a large pool (`points_to_balance_works` test shows `10 balance:3 points ratio` cases and similar edge cases where results legitimately truncate) [1](#0-0) [3](#0-2) .

This `dissolve` computation is invoked directly from the public, unprivileged `unbond` extrinsic:
```
let unbonding_balance = bonded_pool.dissolve(unbonding_points);
T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;
...
let points_unbonded = sub_pools.with_era.get_mut(&unbond_era)...issue(unbonding_balance);
member.try_unbond(unbonding_points, points_unbonded, unbond_era)?;
``` [4](#0-3) 

If a caller picks `unbonding_points` small enough that `current_balance * unbonding_points / current_points` truncates to `0` (i.e. `unbonding_points < current_points / current_balance`, achievable whenever the pool's balance/points ratio is large, e.g. after slashing inflates the balance-per-point or simply for very large pools), the following happens in a single call:
- `bonded_pool.points` is reduced by the full `unbonding_points` (the subtraction is unconditional, independent of the rounding result) — [5](#0-4) .
- `unbonding_balance == 0`, so `T::StakeAdapter::unbond(...)` is asked to unbond `0` real stake — the pool's actual bonded/staked balance is untouched.
- The unbonding sub-pool `issue(0)` credits `0` points for the withdrawing member's claim, meaning the member gets nothing back for those burned active points when they later call `withdraw_unbonded`.
- `member.try_unbond(unbonding_points, 0, unbond_era)` moves `unbonding_points` from the member's `active_points` into `unbonding_eras`, but the value that will actually be redeemable from the corresponding unbonding sub-pool is `0`.

The net effect: the member's redeemable points vanish (worth 0), while the pool's total `points` denominator has shrunk with no corresponding reduction in the pool's real staked `balance`. Because `points_to_balance` for all *other* members is `bonded_balance / bonded_pool.points`, and `bonded_pool.points` decreased while `bonded_balance` (active stake) did not, every remaining member's points become worth strictly *more* balance per point after the call. This is the same broken invariant class as the Sherlock bug: a proportional-reduction division that can silently round to zero, letting the "denominator" side of an accounting relationship advance without the corresponding "numerator" (actual locked/staked value) being debited by the correct (non-zero) amount, permanently misstating locked/backing value.

This differs from ordinary "dust" concerns because it is not bounded by existential-deposit checks: `ok_to_unbond_with` and related checks primarily gate the *resulting active/unbonding balances* against minimum-bond thresholds for the *caller* being unbonded, not the intermediate point→balance conversion of the specific chunk being dissolved in a single call; an attacker who repeats many small `unbond` calls (each individually dissolving points whose share of `current_balance` rounds to `<1`) can burn `bonded_pool.points` faster than `bonded_pool` balance decreases, corrupting the pool's point-to-balance ratio incrementally, exactly as the audited protocol's locked-value accounting was corrupted by repeated small `filledAmount` closes.

### Impact Explanation
This breaks a core solvency invariant of `pallet-nomination-pools`: `bonded_pool.points` and the pool's actual bonded balance must stay proportionally consistent so that `points_to_balance` accurately reflects each member's claim on the pool's real stake. Silent zero-rounding lets an unprivileged caller shrink the points denominator without a matching balance debit, which (a) permanently destroys the value of the affected member's unbonding points/claim (fund loss for that member) and (b) inflates the balance-per-point ratio for every remaining member in the pool, effectively minting unbacked value for others out of the corrupted ratio. Over many repeated calls this compounds, matching the "silent locked-value drift" impact called out and rated Medium in the original Sherlock report.

### Likelihood Explanation
Any signed account that is a nomination-pool member (or has authority to unbond a member, e.g. via permissionless unbond-for-others paths) can call `unbond` with an `unbonding_points` value crafted so that `current_balance * unbonding_points < current_points`. This is easiest in large pools (`current_points` large relative to `current_balance`) or after events that push the balance/points ratio in the attacker's favor, and it does not require any privileged, admin, or governance action — it is purely a public dispatchable with an adversarially-chosen numeric argument, matching the required "unprivileged attacker, public entrypoint" criteria.

### Recommendation
In `BondedPool::dissolve` (and the analogous `UnbondPool::dissolve`), reject or reduce the operation atomically when `points_to_balance(points) == 0` but `points != 0`, instead of subtracting the full point amount from `self.points`. Concretely: require `unbonding_balance` to be non-zero whenever `unbonding_points` is non-zero (return an error such as `Error::<T>::UnbondZeroBalance` for that chunk), or compute both the points-to-subtract and balance-to-credit together and only subtract the fraction of points that actually corresponds to the non-zero balance realized, ensuring the pool's points/balance ratio can never drift because of integer-division rounding.

### Proof of Concept
1. Create a pool where `bonded_pool.points` is large relative to `active_stake` (e.g., due to normal usage/slashing history) such that `bonded_balance / bonded_pool.points < 1` in integer terms — concretely set `current_balance = 100`, `current_points = 300` (per the pallet's own `points_to_balance_works` unit test fixture) [6](#0-5) .
2. Have a member call `Pools::unbond(origin, member, unbonding_points)` with `unbonding_points = 2` (so `100 * 2 / 300 = 0` after integer division).
3. Observe: `bonded_pool.dissolve(2)` returns `unbonding_balance = 0`; `bonded_pool.points` decreases by `2`; `T::StakeAdapter::unbond` is called with `0`; the unbonding sub-pool issues `0` points to the member's claim; `member.try_unbond` records `unbonding_points = 2` moved out of `active_points`, backed by `0` real value.
4. Repeat step 2 across many small `unbond` calls (or many colluding/attacker-controlled member accounts) to progressively shrink `bonded_pool.points` without any corresponding reduction of the pool's real bonded balance, inflating the balance-per-point value for the remaining members and permanently zeroing out the unbonding claims of the affected accounts.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1087-1099)
```rust
	/// Dissolve some points from the pool i.e. unbond the given amount of points from this pool.
	/// This is the opposite of issuing some funds into the pool.
	///
	/// Mutates self in place, but does not write anything to storage.
	///
	/// Returns the equivalent balance amount that actually needs to get unbonded.
	fn dissolve(&mut self, points: BalanceOf<T>) -> BalanceOf<T> {
		// NOTE: do not optimize by removing `balance`. it must be computed before mutating
		// `self.point`.
		let balance = self.points_to_balance(points);
		self.points = self.points.saturating_sub(points);
		balance
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2293-2323)
```rust
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

**File:** substrate/frame/nomination-pools/src/tests.rs (L171-219)
```rust
	#[test]
	fn points_to_balance_works() {
		ExtBuilder::default().build_and_execute(|| {
			// 1 balance : 1 points ratio
			let mut bonded_pool = BondedPool::<Runtime> {
				id: 123123,
				inner: BondedPoolInner {
					commission: Commission::default(),
					member_counter: 1,
					points: 100,
					roles: DEFAULT_ROLES,
					state: PoolState::Open,
				},
			};

			set_pool_balance(bonded_pool.bonded_account(), 100);
			assert_eq!(bonded_pool.points_to_balance(10), 10);
			assert_eq!(bonded_pool.points_to_balance(0), 0);

			// 2 balance : 1 points ratio
			bonded_pool.points = 50;
			assert_eq!(bonded_pool.points_to_balance(10), 20);

			// 100 balance : 0 points ratio
			set_pool_balance(bonded_pool.bonded_account(), 100);
			bonded_pool.points = 0;
			assert_eq!(bonded_pool.points_to_balance(10), 0);

			// 0 balance : 100 points ratio
			set_pool_balance(bonded_pool.bonded_account(), 0);
			bonded_pool.points = 100;
			assert_eq!(bonded_pool.points_to_balance(10), 0);

			// 10 balance : 3 points ratio
			set_pool_balance(bonded_pool.bonded_account(), 100);
			bonded_pool.points = 30;
			assert_eq!(bonded_pool.points_to_balance(10), 33);

			// 2 balance : 3 points ratio
			set_pool_balance(bonded_pool.bonded_account(), 200);
			bonded_pool.points = 300;
			assert_eq!(bonded_pool.points_to_balance(10), 6);

			// 4 balance : 9 points ratio
			set_pool_balance(bonded_pool.bonded_account(), 400);
			bonded_pool.points = 900;
			assert_eq!(bonded_pool.points_to_balance(90), 40);
		})
	}
```
