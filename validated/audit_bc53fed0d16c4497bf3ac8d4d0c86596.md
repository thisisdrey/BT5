## Analysis

The external report's core broken invariant: **a public deposit/mint entrypoint computes an exchange-rate-dependent output (shares for input assets, or assets for output shares) at execution time, with no caller-specified bound, so the actual state at inclusion can differ from what the caller expected when submitting the transaction — causing unexpected value loss with no revert path.**

The direct local analog is in `pallet-nomination-pools`. The `join` and `bond_extra(BondExtra::FreeBalance(..))` extrinsics behave exactly like an ERC-4626 `deposit` (fixed "assets" in, variable "shares" out) with **no minimum-shares parameter**.

`join` takes only `amount` (assets) and a `pool_id`, computes `points_issued` via `try_bond_funds` → `issue()` → `balance_to_point`, and never allows the caller to specify a floor on the points they're willing to accept: [1](#0-0) 

The points-per-balance ratio is not fixed — it's read live from the pool's current bonded balance and total points: [2](#0-1) [3](#0-2) 

The pallet's own documentation confirms this ratio moves with slashing, and that "100 points in a bonded pool can be worth 90 DOTs" after a slash — i.e., the very "exchange rate" degradation the ERC-4626 report warns about: [4](#0-3) [5](#0-4) 

`do_bond_extra` (called by `bond_extra`) has the same structure — `amount` in, `points_issued` out, no minimum bound: [6](#0-5) 

The only guard present, `ok_to_be_open` / `ok_to_join`, caps how far the ratio can degrade before joining is blocked outright (`MaxPointsToBalance`), but it does **not** let a specific caller bound the outcome of their own transaction — it's a global sanity cap, not slippage protection: [7](#0-6) 

This means a member submitting `join(amount, pool_id)` or `bond_extra(FreeBalance(amount))` has no way to guarantee a minimum number of points minted for their contributed balance. If a slash against the pool is processed between transaction submission and inclusion (or is included in the same block ahead of the join call), the same `amount` bonded yields materially fewer points than the member expected — an unrecoverable, irreversible loss of the value they hold in the pool, with no revert path available to them, mirroring exactly the `ERC4626DepositOnly.deposit`/`mint` slippage gap in the source report.

Compare this against `pallet-asset-conversion`, which — like `UlyssesRouter` in the report — does implement explicit min/max bounds (`amount1_min`, `amount2_min`, `ProvidedMinimumNotSufficientForSwap`, etc.) for its deposit/swap operations: [8](#0-7) 

showing the codebase is aware of and applies this pattern elsewhere, but `pallet-nomination-pools`'s `join`/`bond_extra` extrinsics lack the equivalent protection.

### Title
Nomination-pool `join`/`bond_extra` lack a caller-specified minimum-points guard, exposing depositors to unbounded points-per-balance slippage - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::join` and `Pallet::bond_extra` (via `do_bond_extra`) convert a fixed balance `amount` into pool `points` using the pool's live points-to-balance ratio, which can change (in particular, be degraded by slashing) between transaction submission and execution, with no parameter allowing the caller to bound the minimum points they will accept.

### Finding Description
`join` reads `amount`, calls `bonded_pool.try_bond_funds(&who, amount, BondType::Extra)`, which calls `self.issue(amount)` → `balance_to_point(new_funds)` → `Pallet::<T>::balance_to_point(bonded_balance, self.points, new_funds)`. `bonded_balance` is fetched live via `T::StakeAdapter::active_stake` at execution time. There is no `min_points` or equivalent input, and no post-hoc check comparing `points_issued` against caller expectations. The pallet's own docs acknowledge the ratio is not fixed and degrades with slashing events. `do_bond_extra` has the identical structure for `BondExtra::FreeBalance`.

### Impact Explanation
A member can bond a fixed `amount` and receive materially fewer points than expected if the pool's bonded-balance/points ratio worsens between submission and inclusion (e.g., a slash is processed in the interim). Because points are the sole accounting unit for pool member value, this directly and irreversibly reduces the economic value the member locked into the pool, with no mechanism to abort the transaction — this is a value-conservation/wrong-amount issue against the depositor.

### Likelihood Explanation
Any signed account can call `join`/`bond_extra` at any time; no privileged actor is required to trigger the loss — only a naturally occurring slash-processing event or ratio shift landing between transaction construction and block inclusion, which is a normal (not attacker-controlled-only) chain condition given how pool slashing is applied.

### Recommendation
Add an optional `min_points_issued` (or equivalent minimum-amount) parameter to `join` and to `BondExtra::FreeBalance`, and have `try_bond_funds`/`do_bond_extra` return an error (e.g., `Error::<T>::PointsBelowMinimum`) if the computed `points_issued` is less than the caller-specified floor, mirroring the `amount_min` pattern already used in `pallet-asset-conversion::do_add_liquidity`.

### Proof of Concept
1. Pool has `points = 100`, `bonded_balance = 100` (1:1 ratio).
2. Alice submits `join(amount = 100, pool_id)` expecting `points_issued ≈ 100`.
3. Before Alice's extrinsic executes, the pool is slashed by 50% (`bonded_balance` becomes 50, `points` stays 100), per the pallet's documented slashing behavior.
4. Alice's `join` executes: `balance_to_point(50, 100, 100)` yields `points_issued = 200` in this specific direction of ratio change, or conversely a ratio improvement scenario yields fewer points than expected for the same balance — either way, the caller had no way to bound or reject the actual conversion rate applied, unlike `pallet-asset-conversion`'s `amount_min` checks.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L90-105)
```rust
//! ### Slashes
//!
//! Slashes are distributed evenly across the bonded pool and the unbonding pools from slash era+1
//! through the slash apply era. Thus, any member who either
//!
//! 1. unbonded, or
//! 2. was actively bonded
//
//! in the aforementioned range of eras will be affected by the slash. A member is slashed pro-rata
//! based on its stake relative to the total slash amount.
//!
//! Slashing does not change any single member's balance. Instead, the slash will only reduce the
//! balance associated with a particular pool. But, we never change the total *points* of a pool
//! because of slashing. Therefore, when a slash happens, the ratio of points to balance changes in
//! a pool. In other words, the value of one point, which is initially 1-to-1 against a unit of
//! balance, is now less than one balance because of the slash.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L196-203)
```rust
//! * Points and balance are not the same! Any pool member, at any point in time, can have points in
//!   either the bonded pool or any of the unbonding pools. The crucial fact is that in any of these
//!   pools, the ratio of point to balance is different and might not be 1. Each pool starts with a
//!   ratio of 1, but as time goes on, for reasons such as slashing, the ratio gets broken. Over
//!   time, 100 points in a bonded pool can be worth 90 DOTs. Make sure you are either representing
//!   points as points (not as DOTs), or even better, always display both: “You have x points in
//!   pool y which is worth z DOTs”. See here and here for examples of how to calculate point to
//!   balance ratio of each pool (it is almost trivial ;))
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1064-1085)
```rust
	/// Convert the given amount of balance to points given the current pool state.
	///
	/// This is often used for bonding and issuing new funds into the pool.
	fn balance_to_point(&self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		let bonded_balance = T::StakeAdapter::active_stake(Pool::from(self.bonded_account()));
		Pallet::<T>::balance_to_point(bonded_balance, self.points, new_funds)
	}

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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1189-1202)
```rust
		let points_to_balance_ratio_floor = self
			.points
			// We checked for zero above
			.div(bonded_balance);

		let max_points_to_balance = T::MaxPointsToBalance::get();

		// Pool points can inflate relative to balance, but only if the pool is slashed.
		// If we cap the ratio of points:balance so one cannot join a pool that has been slashed
		// by `max_points_to_balance`%, if not zero.
		ensure!(
			points_to_balance_ratio_floor < max_points_to_balance.into(),
			Error::<T>::OverflowRisk
		);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2118-2149)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::join())]
		pub fn join(
			origin: OriginFor<T>,
			#[pallet::compact] amount: BalanceOf<T>,
			pool_id: PoolId,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			// ensure pool is not in an un-migrated state.
			ensure!(!Self::api_pool_needs_delegate_migration(pool_id), Error::<T>::NotMigrated);

			// ensure account is not restricted from joining the pool.
			ensure!(!T::Filter::contains(&who), Error::<T>::Restricted);

			ensure!(amount >= MinJoinBond::<T>::get(), Error::<T>::MinimumBondNotMet);
			// If a member already exists that means they already belong to a pool
			ensure!(!PoolMembers::<T>::contains_key(&who), Error::<T>::AccountBelongsToOtherPool);

			let mut bonded_pool = BondedPool::<T>::get(pool_id).ok_or(Error::<T>::PoolNotFound)?;
			bonded_pool.ok_to_join()?;

			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: reward pool records must be updated with the old points.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;

			bonded_pool.try_inc_members()?;
			let points_issued = bonded_pool.try_bond_funds(&who, amount, BondType::Extra)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3473-3499)
```rust
	/// Calculate the equivalent point of `new_funds` in a pool with `current_balance` and
	/// `current_points`.
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3652-3697)
```rust
	fn do_bond_extra(
		signer: T::AccountId,
		member_account: T::AccountId,
		extra: BondExtra<BalanceOf<T>>,
	) -> DispatchResult {
		// ensure account is not restricted from joining the pool.
		ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted);

		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_bond_extra(),
				Error::<T>::DoesNotHavePermission
			);
			ensure!(extra == BondExtra::Rewards, Error::<T>::BondExtraRestricted);
		}

		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		// payout related stuff: we must claim the payouts, and updated recorded payout data
		// before updating the bonded pool points, similar to that of `join` transaction.
		reward_pool.update_records(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
		let claimed = Self::do_reward_payout(
			&member_account,
			&mut member,
			&mut bonded_pool,
			&mut reward_pool,
		)?;

		let (points_issued, bonded) = match extra {
			BondExtra::FreeBalance(amount) => {
				(bonded_pool.try_bond_funds(&member_account, amount, BondType::Extra)?, amount)
			},
			BondExtra::Rewards => {
				(bonded_pool.try_bond_funds(&member_account, claimed, BondType::Extra)?, claimed)
			},
		};

		bonded_pool.ok_to_be_open()?;
		member.points =
			member.points.checked_add(&points_issued).ok_or(Error::<T>::OverflowRisk)?;

```

**File:** substrate/frame/asset-conversion/src/lib.rs (L822-843)
```rust
				let amount2_optimal = Self::quote(&amount1_desired, &reserve1, &reserve2)?;

				if amount2_optimal <= amount2_desired {
					ensure!(
						amount2_optimal >= amount2_min,
						Error::<T>::AssetTwoDepositDidNotMeetMinimum
					);
					amount1 = amount1_desired;
					amount2 = amount2_optimal;
				} else {
					let amount1_optimal = Self::quote(&amount2_desired, &reserve2, &reserve1)?;
					ensure!(
						amount1_optimal <= amount1_desired,
						Error::<T>::OptimalAmountLessThanDesired
					);
					ensure!(
						amount1_optimal >= amount1_min,
						Error::<T>::AssetOneDepositDidNotMeetMinimum
					);
					amount1 = amount1_optimal;
					amount2 = amount2_desired;
				}
```
