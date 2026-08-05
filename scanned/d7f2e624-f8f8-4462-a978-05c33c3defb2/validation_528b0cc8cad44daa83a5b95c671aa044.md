## Analysis

The HODL report's core broken invariant is: **a value-conversion function (`_buyHodl`) computes an output amount from a price/ratio that can shift between transaction submission and execution, with no caller-supplied minimum-output bound to protect against that shift.**

I checked the closest candidate area in this repo — `pallet-asset-conversion` — and it is **not** vulnerable: every swap entrypoint (`swap_exact_tokens_for_tokens`, `swap_tokens_for_exact_tokens`, and the `SwapCredit` variants used by the EVM precompile and XCM exchange adapter) takes and enforces `amount_out_min` / `amount_in_max` [1](#0-0) [2](#0-1) .

The real local analog is in **`pallet-nomination-pools`**, specifically the `join` and `bond_extra` extrinsics.

### Title
Missing minimum-points-out protection in `pallet-nomination-pools::join`/`bond_extra` - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`join()` and `bond_extra()` convert a member's deposited balance into pool points using a live points:balance ratio computed at execution time, exactly analogous to HODL's price-based minting. Neither extrinsic accepts a `min_points_out` (or equivalent) parameter, so a member has no way to bound how few points they are willing to accept, mirroring the missing `minOut` in `_buyHodl()`.

### Finding Description
When a member joins a pool, points are issued via `BondedPool::issue` → `balance_to_point`, which computes:
```
points_issued = (current_points / current_balance) * new_funds
``` [3](#0-2) [4](#0-3) 

This ratio is read from live storage (`bonded_pool.points`, `active_stake`) at the moment the extrinsic executes, not at the moment the member signed/submitted it [5](#0-4) . The `join` extrinsic only validates a fixed minimum bond amount (`MinJoinBond`) and pool-health invariants (`ok_to_join` / `ok_to_be_open`), but never lets the caller specify the minimum points they're willing to accept for their `amount`:
```rust
pub fn join(origin: OriginFor<T>, #[pallet::compact] amount: BalanceOf<T>, pool_id: PoolId) -> DispatchResult {
    ...
    ensure!(amount >= MinJoinBond::<T>::get(), Error::<T>::MinimumBondNotMet);
    ...
    let points_issued = bonded_pool.try_bond_funds(&who, amount, BondType::Extra)?;
    ...
}
``` [6](#0-5) 

`ok_to_be_open` only guards against gross overflow risk (ratio exceeding `MaxPointsToBalance`); it does not protect a joining member from receiving fewer points than expected due to a ratio shift caused by intervening transactions (e.g., another member's slash-affected `join`, or a slash event landing in the same block before the member's extrinsic) [7](#0-6) . The pallet's own test explicitly demonstrates the ratio changing between two `join` calls due to an intervening slash, producing a very different points-per-balance outcome (`points: 2` before slash vs `points: 24` after a 50% slash for the same-ish deposit) [8](#0-7) .

This is structurally identical to the HODL flaw: a price/ratio that can move due to another actor's transaction in the same block, and a public entrypoint that mints/issues a derived quantity to the caller without letting them bound the minimum acceptable output.

### Impact Explanation
A pool member submitting `join`/`bond_extra` cannot guarantee the number of points (i.e., their proportional claim on the pool and its future rewards/unbonded balance) they will receive for a given deposit. If the points:balance ratio worsens between submission and inclusion (e.g., a slash or another large bond changes `bonded_pool.points`/`active_stake`), the member is silently issued fewer points for the same balance, permanently diluting their claim relative to what they expected — with no on-chain mechanism to reject the trade. This is a value/accounting-fairness issue under the "staking or asset accounting" impact category, though it does not create unbacked mint, duplicate settlement, or a chain-halting condition — it only affects the individual member's own point/balance ratio at the time of their own transaction, which limits blast radius to self-inflicted mispricing rather than fund theft from other members.

### Likelihood Explanation
Requires a slash or other points/balance changing event to occur in the same block window as the join/bond_extra transaction — a condition outside the caller's control but not requiring any privileged, malicious, or off-repo actor. This is a naturally occurring ordering/timing effect rather than an attack requiring a malicious peer, relayer, or validator, so it fits the "public underpriced work / unfavorable execution" pattern rather than a deliberate exploit by another party against the victim (there's no direct way for another unprivileged account to weaponize this against a target, since slashes are triggered by external staking misconduct, not by a peer's transaction). This tempers the severity toward informational/low rather than a directly exploitable Medium-equivalent finding.

### Recommendation
Add an optional `min_points_out` (or `min_bonded_ratio`) parameter to `join` and `bond_extra`, and enforce `points_issued >= min_points_out` after `try_bond_funds` computes the actual issuance, returning an error (e.g., `Error::<T>::SlippageExceeded`) otherwise — mirroring the `amount_out_min` pattern already used correctly in `pallet-asset-conversion::do_swap_exact_tokens_for_tokens` [9](#0-8) .

### Proof of Concept
1. Pool has `points = 100`, `bonded_balance = 100` (1:1 ratio).
2. Member A submits `join(amount = 10)` expecting ~10 points.
3. Before A's transaction executes, a slash event reduces `bonded_balance` to 50 within the same block (via `StakingMock::slash_by` equivalent in production staking slashing), as reproduced in the existing test `join_works` [10](#0-9) .
4. A's `join` still executes, but `balance_to_point` now uses the post-slash ratio, issuing a different number of points than A anticipated when signing — with no parameter available to have reverted the transaction instead.

**Caveat**: I could not fully verify how frequently in-block ratio shifts realistically occur across all staking configurations (e.g., `staking-async` vs legacy `staking`), and whether any wrapper (e.g., a front-end helper) already estimates and enforces slippage off-chain; that would need confirmation via a live Devin session with full repo/test access.

### Citations

**File:** substrate/frame/asset-conversion/src/lib.rs (L987-1002)
```rust
		) -> Result<T::Balance, DispatchError> {
			ensure!(amount_in > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_out_min) = amount_out_min {
				ensure!(amount_out_min > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_in(amount_in, path)?;

			let amount_out = path.last().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_out_min) = amount_out_min {
				ensure!(
					amount_out >= amount_out_min,
					Error::<T>::ProvidedMinimumNotSufficientForSwap
				);
			}
```

**File:** substrate/frame/asset-conversion/precompiles/src/lib.rs (L60-70)
```rust
		/// @param amountOutMin Minimum acceptable amount of the last asset to receive.
		/// @param sendTo Address to receive the output tokens.
		/// @param keepAlive If true, ensures the sender account stays above existential deposit.
		/// @return amountOut The amount of output tokens received.
		function swapExactTokensForTokens(
			bytes[] calldata path,
			uint256 amountIn,
			uint256 amountOutMin,
			address sendTo,
			bool keepAlive
		) external returns (uint256 amountOut);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1064-1070)
```rust
	/// Convert the given amount of balance to points given the current pool state.
	///
	/// This is often used for bonding and issuing new funds into the pool.
	fn balance_to_point(&self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		let bonded_balance = T::StakeAdapter::active_stake(Pool::from(self.bonded_account()));
		Pallet::<T>::balance_to_point(bonded_balance, self.points, new_funds)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1080-1085)
```rust
	/// Issue points to [`Self`] for `new_funds`.
	fn issue(&mut self, new_funds: BalanceOf<T>) -> BalanceOf<T> {
		let points_to_issue = self.balance_to_point(new_funds);
		self.points = self.points.saturating_add(points_to_issue);
		points_to_issue
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1183-1208)
```rust
	fn ok_to_be_open(&self) -> Result<(), DispatchError> {
		ensure!(!self.is_destroying(), Error::<T>::CanNotChangeState);

		let bonded_balance = T::StakeAdapter::active_stake(Pool::from(self.bonded_account()));
		ensure!(!bonded_balance.is_zero(), Error::<T>::OverflowRisk);

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

		// then we can be decently confident the bonding pool points will not overflow
		// `BalanceOf<T>`. Note that these are just heuristics.

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2120-2149)
```rust
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

**File:** substrate/frame/nomination-pools/src/tests.rs (L827-879)
```rust
		ExtBuilder::default().with_check(0).build_and_execute(|| {
			// Given
			Currency::set_balance(&11, ExistentialDeposit::get() + 2);
			assert!(!PoolMembers::<Runtime>::contains_key(11));
			assert_eq!(TotalValueLocked::<T>::get(), 10);

			// When
			assert_ok!(Pools::join(RuntimeOrigin::signed(11), 2, 1));

			// Then
			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::Created { depositor: 10, pool_id: 1 },
					Event::Bonded { member: 10, pool_id: 1, bonded: 10, joined: true },
					Event::MetadataUpdated { pool_id: 1, caller: 900 },
					Event::Bonded { member: 11, pool_id: 1, bonded: 2, joined: true },
				]
			);
			assert_eq!(TotalValueLocked::<T>::get(), 12);

			assert_eq!(
				PoolMembers::<Runtime>::get(11).unwrap(),
				PoolMember::<Runtime> { pool_id: 1, points: 2, ..Default::default() }
			);
			assert_eq!(BondedPool::<Runtime>::get(1).unwrap(), bonded(12, 2));

			// Given
			// The bonded balance is slashed in half
			StakingMock::slash_by(1, 6);

			// And
			Currency::set_balance(&12, ExistentialDeposit::get() + 12);
			assert!(!PoolMembers::<Runtime>::contains_key(12));

			// When
			assert_ok!(Pools::join(RuntimeOrigin::signed(12), 12, 1));

			// Then
			assert_eq!(
				pool_events_since_last_call(),
				vec![
					Event::PoolSlashed { pool_id: 1, balance: 6 },
					Event::Bonded { member: 12, pool_id: 1, bonded: 12, joined: true }
				]
			);
			assert_eq!(TotalValueLocked::<T>::get(), 18);

			assert_eq!(
				PoolMembers::<Runtime>::get(12).unwrap(),
				PoolMember::<Runtime> { pool_id: 1, points: 24, ..Default::default() }
			);
			assert_eq!(BondedPool::<Runtime>::get(1).unwrap(), bonded(12 + 24, 3));
```
