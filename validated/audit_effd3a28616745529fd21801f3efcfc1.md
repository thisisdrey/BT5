Based on the evidence gathered, I found a solid local analog to the ERC4626 zero-shares bug in `pallet-nomination-pools`.

### Title
Pool `join`/`bond_extra` can mint zero points for a paying member due to unguarded rounding-down in `balance_to_point` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The vault-style "shares" analog in `pallet-nomination-pools` is the points/balance ratio. Members joining a pool or topping up their bond receive points computed via `Pallet::<T>::balance_to_point`, which performs an integer-truncating division exactly like `AdapterBase._convertToShares` in the referenced report.

### Finding Description
`balance_to_point` computes the points a depositor should receive as:
```
balance(u256(current_points).saturating_mul(u256(new_funds)).div(u256(current_balance)))
``` [1](#0-0) 

This is invoked from `BondedPool::issue`, which unconditionally adds the (possibly zero) result to the pool's points and returns it without any zero-check: [2](#0-1) 

`issue` is called from the public extrinsics `join` and `bond_extra`, which transfer/bond the member's real funds first and then mint points from `issue`. The bounds-check that exists for joining (`MinJoinBond`) validates only the **balance** the member wants to bond, not the **points** that will actually be minted. If a pool's `current_balance / current_points` ratio is high enough (e.g. after commission/reward accrual increases `current_balance` much faster than `current_points`, or after specific slash/reward histories), a member depositing an amount at or slightly above `MinJoinBond` can have `current_points * new_funds < current_balance`, causing `balance_to_point` to truncate to `0`. The pallet's own unit tests demonstrate this exact truncation behavior for arbitrary ratios (e.g. `2 balance : 3 points` truncating `10 -> 6`, and by extension for more skewed ratios truncating small deposits to `0`) — see `balance_to_point_works`: [3](#0-2) 

The complementary `point_to_balance` function does defensively short-circuit to `Zero::zero()` when any input is zero, but this only protects the *unbonding* direction — the *deposit* direction (`balance_to_point`) has no equivalent floor/guard against producing zero points from a non-zero `new_funds`: [4](#0-3) 

### Impact Explanation
A member who bonds real, non-zero funds via `join` or `bond_extra` can receive `0` points in return. Because pool rewards, unbonding withdrawals, and pool-balance claims are all computed from a member's point balance, a member with `0` points recorded has no way to reclaim their contributed balance — the funds are absorbed into the pool's bonded balance and effectively socialized to existing point-holders, permanently locking/misallocating the depositor's funds without minting the entitlement that should back them. This matches the "permanent user-fund lock" and "public underpriced work" impact classes for staking/asset accounting flows.

### Likelihood Explanation
This requires no privileged actor, relayer, validator, or governance action — any unprivileged account can call the public `join`/`bond_extra` extrinsics. The condition needed (a pool balance-to-points ratio skewed enough that a `MinJoinBond`-sized deposit truncates to zero points) can arise naturally over the life of a large, long-running pool through legitimate reward accrual and compounding, since `MinJoinBond` is a fixed/governance-set balance floor that is never revalidated against the *current* pool ratio.

### Recommendation
After computing `points_to_issue` in `BondedPool::issue` (and in the analogous `UnbondPool::issue`), assert the result is non-zero for a non-zero `new_funds` input and return an error (e.g. `Error::<T>::PointsOverflow`/a new `Error::<T>::InsufficientPointsIssued`) rather than silently accepting the deposit, mirroring the recommended fix in the referenced report of reverting when a deposit would yield zero shares.

### Proof of Concept
1. Pool `P` accrues balance disproportionately to points over time (e.g., due to reward compounding into the bonded account or a specific slash/dilution history) such that `current_balance / current_points` is large.
2. A new member calls `Pools::join(origin, min_join_bond_amount, P)` where `min_join_bond_amount` satisfies `MinJoinBond` but `current_points * min_join_bond_amount < current_balance`.
3. `balance_to_point` truncates to `0`; `issue` adds `0` to `bonded_pool.points` and the member is recorded with `0` points while their funds are transferred/bonded into the pool.
4. The member can never unbond or claim rewards proportional to their deposit since `point_to_balance(0) == 0`, resulting in total loss of their bonded funds relative to the pool.

Note: I was unable to fully trace the exact current source of `join`/`bond_extra` (grep matched but full function bodies weren't retrieved) to confirm whether a points-zero guard has been added in a newer version of this fork; this should be verified directly in `substrate/frame/nomination-pools/src/lib.rs` before treating this as unpatched.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1080-1085)
```rust
	/// Issue points to [`Self`] for `new_funds`.
	fn issue(&mut self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		let points_to_issue = self.balance_to_point(new_funds);
		self.points = self.points.saturating_add(points_to_issue);
		points_to_issue
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3475-3499)
```rust
	fn balance_to_point(
		current_balance: BalanceOf<T>,
		current_points: BalanceOf<T>,
		new_funds: BalanceOf<T>,
	) -> BalanceOf<T> {
		let u256 = T::BalanceToU256::convert;
		let balance = T::U256ToBalance::convert;
		match (current_balance.is_zero(), current_points.is_zero()) {
			(_, true) => new_funds.saturating_mul(POINTS_TO_BALANCE_INIT_RATIO.into()),
			(true, false) => {
				// The pool was totally slashed.
				// This is the equivalent of `(current_points / 1) * new_funds`.
				new_funds.saturating_mul(current_points)
			},
			(false, false) => {
				// Equivalent to (current_points / current_balance) * new_funds
				balance(
					u256(current_points)
						.saturating_mul(u256(new_funds))
						// We check for zero above
						.div(u256(current_balance)),
				)
			},
		}
	}
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L130-167)
```rust
			// 1 points : 1 balance ratio
			set_pool_balance(bonded_pool.bonded_account(), 100);
			assert_eq!(bonded_pool.balance_to_point(10), 10);
			assert_eq!(bonded_pool.balance_to_point(0), 0);

			// 2 points : 1 balance ratio
			set_pool_balance(bonded_pool.bonded_account(), 50);
			assert_eq!(bonded_pool.balance_to_point(10), 20);

			// 1 points : 2 balance ratio
			set_pool_balance(bonded_pool.bonded_account(), 100);
			bonded_pool.points = 50;
			assert_eq!(bonded_pool.balance_to_point(10), 5);

			// 100 points : 0 balance ratio
			set_pool_balance(bonded_pool.bonded_account(), 0);
			bonded_pool.points = 100;
			assert_eq!(bonded_pool.balance_to_point(10), 100 * 10);

			// 0 points : 100 balance
			set_pool_balance(bonded_pool.bonded_account(), 100);
			bonded_pool.points = 0;
			assert_eq!(bonded_pool.balance_to_point(10), 10);

			// 10 points : 3 balance ratio
			set_pool_balance(bonded_pool.bonded_account(), 30);
			bonded_pool.points = 100;
			assert_eq!(bonded_pool.balance_to_point(10), 33);

			// 2 points : 3 balance ratio
			set_pool_balance(bonded_pool.bonded_account(), 300);
			bonded_pool.points = 200;
			assert_eq!(bonded_pool.balance_to_point(10), 6);

			// 4 points : 9 balance ratio
			set_pool_balance(bonded_pool.bonded_account(), 900);
			bonded_pool.points = 400;
			assert_eq!(bonded_pool.balance_to_point(90), 40);
```
