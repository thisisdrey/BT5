### Title
`pallet-nomination-pools::join`/`bond_extra` accept funds at a points-to-balance exchange rate with no member-supplied minimum-points (slippage) bound - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The external report's core defect is that a party committing funds/collateral to a contract accepts externally-derived pricing terms (rate, duration, liquidation price) computed at execution time with no ability to bound the acceptable range, so the terms can shift between submission and execution to the depositor's detriment. The same broken invariant exists in `pallet-nomination-pools`: a member calling `join`/`bond_extra` transfers balance into a pool and is issued points computed from the pool's *current* `bonded_balance`/`points` ratio at execution time, with no parameter letting the member specify a minimum number of points (or a bound on the effective price-per-point) they are willing to accept.

### Finding Description
When a member joins a bonded pool, points are minted via `balance_to_point`, which derives the exchange rate purely from state read at the moment the extrinsic executes: [1](#0-0) 

This is invoked by `BondedPool::issue`/`balance_to_point`, used from the `join` and `bond_extra` call paths: [2](#0-1) 

The doc comments explicitly acknowledge the ratio is meant to stay constant across a transfer, but nothing in the call enforces that the *caller's expected* ratio matches the ratio actually applied: [3](#0-2) 

Tests confirm the ratio is state-dependent and can move sharply between the time a member observes it off-chain and the time their extrinsic lands — a 50% slash halves the balance, doubling the points-per-balance rate for the next joiner, with no way for that joiner to reject the new rate: [4](#0-3) 

Contrast this with `pallet-asset-conversion`, which implements exactly the recommendation from the external report (caller-supplied `amount_out_min`/`amount_in_max` bounds enforced before settlement): [5](#0-4) [6](#0-5) 

`pallet-nomination-pools`'s `join`/`bond_extra` calls have no analogous `min_points_out` (or equivalent) guard — the only checks are `MinJoinBond`/`OverflowRisk`, which bound absolute amounts, not the price ratio the depositor receives: [7](#0-6) 

### Impact Explanation
A member's bonded balance is convertible back to a payout only through the same points/balance ratio (`points_to_balance`), so receiving fewer points than expected for a given deposit is a direct, permanent economic loss of principal share in the pool — this is the "Balances, staking, ... must conserve value and settle exactly once to the rightful beneficiary and amount" category from the impact gate. Because the exchange rate is read at execution time from mutable on-chain state (`bonded_balance`, `points`), and the extrinsic offers no bound, the depositor has no on-chain enforceable guarantee about how many points they will actually receive for their transferred balance.

### Likelihood Explanation
Any unprivileged account can call `join`/`bond_extra` at any time; the effective points/balance ratio can move between the moment a user decides to deposit and the moment the extrinsic is included and executed, since it depends on pool state that mutates from ordinary pool activity between blocks. Unlike the asset-conversion pallet in the same codebase — which was hardened with explicit min/max bound parameters for this exact class of issue — nomination-pools has no such parameter, so the gap is a straightforward, provable omission relative to the established pattern already used elsewhere in this repository.

### Recommendation
Add an optional `min_points_out` (or equivalent bound on the applied points-per-balance rate) parameter to `join` and `bond_extra`, mirroring `amount_out_min`/`amount_in_max` in `pallet-asset-conversion::swap_exact_tokens_for_tokens`/`swap_tokens_for_exact_tokens`. After computing `points_to_issue` via `balance_to_point`, `ensure!(points_to_issue >= min_points_out, Error::<T>::SlippageExceeded)` before mutating storage, so the transaction reverts instead of silently minting an unfavorable amount of points.

### Proof of Concept
1. Pool P has `bonded_balance = 100`, `points = 100` (1:1 ratio).
2. User A observes this ratio off-chain and submits `Pools::join(A, 100, P)` expecting ~100 points.
3. Before A's extrinsic executes, `bonded_balance` for P is reduced (e.g., via a slash event processed in the same or an earlier block), making `balance_to_point` return double the points per balance as shown in `slash_no_subpool_is_tracked`: [4](#0-3) 
4. A's `join` executes against the new ratio with no on-chain check that the outcome matches A's expectation, and A had no parameter available to constrain or reject the unfavorable rate — the transaction that would revert under a slippage-guard design (as in asset-conversion) instead succeeds silently in nomination-pools.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L240-252)
```rust
//! When the pool already has some balance, we want the value of a point before the transfer to
//! equal the value of a point after the transfer. So, when a member joins a bonded pool with a
//! given `amount_transferred`, we maintain the ratio of bonded balance to points such that:
//!
//! ```text
//! balance_after_transfer / points_after_transfer == balance_before_transfer / points_before_transfer;
//! ```
//!
//! To achieve this, we issue points based on the following:
//!
//! ```text
//! points_issued = (points_before_transfer / balance_before_transfer) * amount_transferred;
//! ```
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

**File:** substrate/frame/nomination-pools/src/tests.rs (L883-930)
```rust
	#[test]
	fn join_errors_correctly() {
		ExtBuilder::default().with_check(0).build_and_execute(|| {
			// 10 is already part of the default pool created.
			assert_eq!(PoolMembers::<Runtime>::get(10).unwrap().pool_id, 1);

			assert_noop!(
				Pools::join(RuntimeOrigin::signed(10), 420, 123),
				Error::<Runtime>::AccountBelongsToOtherPool
			);

			assert_noop!(
				Pools::join(RuntimeOrigin::signed(11), 420, 123),
				Error::<Runtime>::PoolNotFound
			);

			// Force the pools bonded balance to 0, simulating a 100% slash
			set_pool_balance(Pools::generate_bonded_account(1), 0);
			assert_noop!(
				Pools::join(RuntimeOrigin::signed(11), 420, 1),
				Error::<Runtime>::OverflowRisk
			);

			// Given a mocked bonded pool
			BondedPool::<Runtime> {
				id: 123,
				inner: BondedPoolInner {
					commission: Commission::default(),
					member_counter: 1,
					points: 100,
					roles: DEFAULT_ROLES,
					state: PoolState::Open,
				},
			}
			.put();

			// and reward pool
			RewardPools::<Runtime>::insert(123, RewardPool::<Runtime> { ..Default::default() });

			// Force the points:balance ratio to `MaxPointsToBalance` (100/10)
			let max_points_to_balance: u128 =
				<<Runtime as Config>::MaxPointsToBalance as Get<u8>>::get().into();

			set_pool_balance(Pools::generate_bonded_account(123), max_points_to_balance);
			assert_noop!(
				Pools::join(RuntimeOrigin::signed(11), 420, 123),
				Error::<Runtime>::OverflowRisk
			);
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L7736-7761)
```rust
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

**File:** substrate/frame/asset-conversion/src/lib.rs (L519-545)
```rust
		/// Swap the exact amount of `asset1` into `asset2`.
		/// `amount_out_min` param allows you to specify the min amount of the `asset2`
		/// you're happy to receive.
		///
		/// [`AssetConversionApi::quote_price_exact_tokens_for_tokens`] runtime call can be called
		/// for a quote.
		#[pallet::call_index(3)]
		#[pallet::weight(T::WeightInfo::swap_exact_tokens_for_tokens(path.len() as u32))]
		pub fn swap_exact_tokens_for_tokens(
			origin: OriginFor<T>,
			path: Vec<Box<T::AssetKind>>,
			amount_in: T::Balance,
			amount_out_min: T::Balance,
			send_to: T::AccountId,
			keep_alive: bool,
		) -> DispatchResult {
			let sender = ensure_signed(origin)?;
			Self::do_swap_exact_tokens_for_tokens(
				sender,
				path.into_iter().map(|a| *a).collect(),
				amount_in,
				Some(amount_out_min),
				send_to,
				keep_alive,
			)?;
			Ok(())
		}
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L988-1002)
```rust
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
