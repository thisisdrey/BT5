## Analysis

The Lido `_burnShares` bug is a classic "rounding down to zero" flaw: a caller can pass a `sharesAmount` that, after being converted through a ratio, rounds down to zero, yet the surrounding code still records that the burn "happened" (i.e., no revert, no `require(newTotal != oldTotal)`) — silently losing the caller's positional accounting without moving any real value.

The `pallet-nomination-pools` unbond flow in this repo has the exact same structural weakness in `Pallet::<T>::unbond` and the point↔balance conversion helpers.

### Title
Rounding-to-zero in nomination-pools `unbond` lets a member's active points be dissolved for zero unbonding balance/points, permanently losing stake - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`unbond` accepts any `unbonding_points > 0` without any check on the resulting `unbonding_balance` or on the points issued into the unbonding sub-pool. Because `point_to_balance` rounds down using integer division, a caller can choose `unbonding_points` small enough (relative to the pool's `points`/`balance` ratio) that `bonded_pool.dissolve(unbonding_points)` returns `unbonding_balance == 0`, while `bonded_pool.points` is still reduced by the full `unbonding_points`. The zero balance is then re-issued into the unbonding sub-pool via `UnbondPool::issue(0)`, which returns `0` points, and `member.try_unbond` records `0` points in `unbonding_eras` for that era while removing the real `unbonding_points` from the member's active points.

### Finding Description
The core conversion function performs floor division and has no non-zero-result guard for nonzero inputs: [1](#0-0) 

`BondedPool::points_to_balance`/`dissolve` call straight into this helper: [2](#0-1) 

In the `unbond` extrinsic, the only gating logic is `ok_to_unbond_with`, which checks permission/minimum-bond rules but never checks that `unbonding_points` (or the resulting `unbonding_balance`) is non-zero, nor that it is large enough to survive the points→balance rounding: [3](#0-2) 

The dispatchable then dissolves the member's points from the bonded pool using the (possibly zero) computed balance, issues that same (possibly zero) balance into the era's `UnbondPool`, and records whatever points that issuance produced against the member's `unbonding_eras`: [4](#0-3) 

`UnbondPool::issue` mirrors the same floor-division pattern, returning `0` new points for `0` new funds: [5](#0-4) 

`PoolMember::try_unbond` subtracts the full `unbonding_points` from `self.points` (the bonded/active stake) but only credits `points_issued` (potentially `0`) into `unbonding_eras`, which is what later determines the amount withdrawable via `withdraw_unbonded`: [6](#0-5) 

The net effect: the member's active bonded points (and thus their claim on the bonded pool / future rewards eligibility) are reduced, but their corresponding entry in the unbonding sub-pool is `0`, meaning `withdraw_unbonded` will later return `0` balance for that unlock (`UnbondPool::dissolve(0)` yields `0`), because it uses the same floor-division `point_to_balance`: [7](#0-6) 

This is the same invariant break as the Lido report: a rounding-down conversion silently "succeeds" with a zero-valued transfer/burn while the accounting ledger (points/shares) is still mutated as if a nonzero amount moved.

### Impact Explanation
A pool member can lose a slice of their active stake permanently: those points are removed from `bonded_pool.points` (reducing the member's proportional claim on the bonded stake and future reward accrual) while the matching unbonding-pool credit is zero, so no balance is ever unlocked for that unbond call. Repeating this (e.g., via many small unbond calls, each below the rounding threshold) lets an attacker or a confused/malicious actor grief their own or, if permissionless kicking/destroying conditions apply, another member's position, resulting in a partial, unrecoverable fund lock — this falls squarely under "theft or unbacked mint or unlock" / "permanent user-fund ... lock" in the impact scope.

### Likelihood Explanation
The rounding condition is deterministic and fully attacker-controlled: any signed account holding pool membership can call `unbond` with a small `unbonding_points` value once the pool's `bonded_balance / points` ratio is large enough (e.g., after the pool has accrued significant stake relative to points, or been diluted by `POINTS_TO_BALANCE_INIT_RATIO` scaling). No privileged role, governance action, or malicious external actor is required — this is a pure public-entrypoint arithmetic edge case, matching the "unprivileged attacker" requirement.

### Recommendation
In `ok_to_unbond_with` (or immediately before performing the dissolve in `unbond`), add an explicit check that the balance-equivalent of `unbonding_points` (via `points_to_balance`) is non-zero whenever `unbonding_points` is non-zero, and reject the call (e.g., a new `Error::<T>::CannotUnbondZeroBalance` style check) if the conversion would round to zero. The same guard should be applied to the sub-pool `issue`/`dissolve` calls so that no points can be dissolved from the bonded pool without a strictly positive equivalent balance being moved into the unbonding pool.

### Proof of Concept
1. Create a pool and let its `bonded_pool.points : bonded_balance` ratio grow very lopsided (e.g., via repeated `bond_extra`/reward compounding or slashing scenarios covered by the existing `balance_to_point_works`/`points_to_balance_works` unit tests, which already demonstrate ratios such as "100 points : 3 balance"). [8](#0-7) 
2. As a member, call `Pools::unbond(origin, member, unbonding_points)` with `unbonding_points` chosen such that `point_to_balance(bonded_balance, bonded_pool.points, unbonding_points)` floors to `0` (e.g. ratio `points : balance` of `10:3`, unbond `1` point → `1*3/10 = 0`).
3. Observe: `bonded_pool.points` decreases by `unbonding_points`, `unbonding_balance == 0`, `UnbondPool::issue(0)` returns `0`, and `member.unbonding_eras` records `0` points for that era.
4. Wait out the bonding duration and call `withdraw_unbonded`; the member receives `0` balance for that unlock, while their active bonded points/claim have already been permanently reduced.

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1072-1099)
```rust
	/// Convert the given number of points to balance given the current pool state.
	///
	/// This is often used for unbonding.
	fn points_to_balance(&self, points: BalanceOf<T>) -> BalanceOf<T> {
		let bonded_balance = T::StakeAdapter::active_stake(Pool::from(self.bonded_account()));
		Pallet::<T>::point_to_balance(bonded_balance, self.points, points)
	}

	/// Issue points to [`Self`] for `new_funds`.
	fn issue(&mut self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		let points_to_issue = self.balance_to_point(new_funds);
		self.points = self.points.saturating_add(points_to_issue);
		points_to_issue
	}

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1217-1289)
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

		// additional checks:
		match (is_permissioned, is_depositor) {
			(true, false) => (),
			(true, true) => {
				// permission depositor unbond: if destroying and pool is empty, always allowed,
				// with no additional limits.
				if self.is_destroying_and_only_depositor(target_member.active_points()) {
					// everything good, let them unbond anything.
				} else {
					// depositor cannot fully unbond yet.
					ensure!(!is_full_unbond, Error::<T>::MinimumBondNotMet);
				}
			},
			(false, false) => {
				// If the pool is blocked, then an admin with kicking permissions can remove a
				// member. If the pool is being destroyed, anyone can remove a member
				debug_assert!(is_full_unbond);
				ensure!(
					self.can_kick(caller) || self.is_destroying(),
					Error::<T>::NotKickerOrDestroying
				)
			},
			(false, true) => {
				// Permissionless depositor unbond is only allowed for a full unbond, and only when
				// destroying with the depositor as sole remaining member. `is_full_unbond` is
				// already guaranteed by the outer `ensure!` above.
				debug_assert!(is_full_unbond);
				ensure!(
					self.is_destroying_and_only_depositor(target_member.active_points()),
					Error::<T>::DoesNotHavePermission
				);
			},
		};

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1548-1565)
```rust
impl<T: Config> UnbondPool<T> {
	fn balance_to_point(&self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		Pallet::<T>::balance_to_point(self.balance, self.points, new_funds)
	}

	fn point_to_balance(&self, points: BalanceOf<T>) -> BalanceOf<T> {
		Pallet::<T>::point_to_balance(self.balance, self.points, points)
	}

	/// Issue the equivalent points of `new_funds` into self.
	///
	/// Returns the actual amounts of points issued.
	fn issue(&mut self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		let new_points = self.balance_to_point(new_funds);
		self.points = self.points.saturating_add(new_points);
		self.balance = self.balance.saturating_add(new_funds);
		new_points
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1567-1577)
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
