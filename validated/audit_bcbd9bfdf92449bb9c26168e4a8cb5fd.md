Audit Report

## Title
Unchecked Zero-Point Issuance in `try_bond_funds` Can Silently Lock Bonded Funds With No Pool Credit - ([File: substrate/frame/nomination-pools/src/lib.rs])

## Summary
`BondedPool::try_bond_funds` issues pool points via `self.issue(amount)` — which internally performs a truncating integer division in `balance_to_point` — and stakes/transfers the member's funds unconditionally, without ever verifying that the resulting `points_issued` is nonzero for a nonzero `amount`. Because all callers (`join`, `do_bond_extra`, `do_create`) trust this return value and store it directly as member points, a caller whose deposit truncates to zero points has their funds staked and counted in `TotalValueLocked` while receiving no claim on the pool, permanently losing access to the deposited value.

## Finding Description
`try_bond_funds` computes `points_issued = self.issue(amount)` before performing the actual stake/transfer via `T::StakeAdapter::pledge_bond`, and returns `points_issued` with no zero-check gating the bond itself. [1](#0-0) 

`issue` calls `balance_to_point`, whose mirror function `point_to_balance` documents the truncating `u256` division explicitly (`current_balance / current_points * points`), which can floor to zero for small inputs when the balance:points ratio is large. [2](#0-1) [3](#0-2) 

The public `join` extrinsic path stores the returned `points_issued` directly as the new member's `points` with no validation that it is nonzero. [4](#0-3) 

`do_bond_extra` and `do_create` exhibit the identical pattern, unconditionally adding `points_issued` to member points regardless of whether it is zero. [5](#0-4) [6](#0-5) 

The pallet's own `ok_to_join` guard against `OverflowRisk` bounds the pool's balance:points ratio only to prevent extreme dilution in one direction, as demonstrated by `ok_to_join_with_works`, but this bounds the *pool-level* ratio, not the interaction between that ratio and a specific small `amount` at bond time — a nonzero amount can still truncate to zero points within an "acceptable" ratio. [7](#0-6) 

For comparison, `pallet_asset_conversion::do_add_liquidity` explicitly guards against exactly this class of bug by rejecting mints that fall at or below a minimum threshold before minting LP tokens, a pattern absent for pool points. [8](#0-7) 

## Impact Explanation
This is a value-conservation violation: a depositor's funds are staked and reflected in `TotalValueLocked` and the pool's bonded balance, but the corrupted value — the depositor's `PoolMember.points` — remains zero, giving them no redeemable claim. Since unbonding/withdrawal in nomination-pools is points-denominated, the depositor cannot recover the staked value, and it is effectively redistributed pro-rata to all other pool members. This matches the "permanent user-fund lock" / value-not-conserved impact class, reachable via the unprivileged `join`/`bond_extra`/`bond_extra_other` extrinsics.

## Likelihood Explanation
Exploitability is conditional, not universal: it requires the pool's balance:points ratio to have drifted (via slashing history or asymmetric growth) to a point where a legitimate, otherwise-valid bond `amount` truncates to zero points under `balance_to_point`'s integer division, while still remaining within the `MaxPointsToBalance`-bounded range accepted by `ok_to_join`. This is state-dependent but requires no privileged access — any account performing `join` or `bond_extra` against a pool in this state triggers the bug deterministically.

## Recommendation
Add an explicit check immediately after `self.issue(amount)` inside `try_bond_funds` that rejects the bond when `points_issued` is zero for a nonzero `amount`, e.g. `ensure!(!points_issued.is_zero() || amount.is_zero(), Error::<T>::InsufficientBond);`, mirroring the `InsufficientLiquidityMinted` guard in `pallet_asset_conversion::do_add_liquidity`.

## Proof of Concept
1. Establish a pool whose bonded balance:points ratio is large due to slashing/growth history, while remaining within `ok_to_join`'s `MaxPointsToBalance` bound.
2. Call `Pools::join(origin, amount, pool_id)` with an `amount` small enough that `balance_to_point(bonded_balance, points, amount)` truncates to `0`.
3. Observe `try_bond_funds` stakes/transfers `amount` (increasing `TotalValueLocked` and the pool's staked balance) while returning `points_issued == 0`.
4. Observe `PoolMembers::<T>::insert` stores the member with `points: 0`, confirming the deposited value is unrecoverable by the depositor. [1](#0-0) [4](#0-3)

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1315-1337)
```rust
	fn try_bond_funds(
		&mut self,
		who: &T::AccountId,
		amount: BalanceOf<T>,
		ty: BondType,
	) -> Result<BalanceOf<T>, DispatchError> {
		// We must calculate the points issued *before* we bond who's funds, else points:balance
		// ratio will be wrong.
		let points_issued = self.issue(amount);

		T::StakeAdapter::pledge_bond(
			Member::from(who.clone()),
			Pool::from(self.bonded_account()),
			&self.reward_account(),
			amount,
			ty,
		)?;
		TotalValueLocked::<T>::mutate(|tvl| {
			tvl.saturating_accrue(amount);
		});

		Ok(points_issued)
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2148-2161)
```rust
			bonded_pool.try_inc_members()?;
			let points_issued = bonded_pool.try_bond_funds(&who, amount, BondType::Extra)?;

			PoolMembers::insert(
				who.clone(),
				PoolMember::<T> {
					pool_id,
					points: points_issued,
					// we just updated `last_known_reward_counter` to the current one in
					// `update_recorded`.
					last_recorded_reward_counter: reward_pool.last_recorded_reward_counter(),
					unbonding_eras: Default::default(),
				},
			);
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3604-3605)
```rust
		bonded_pool.try_inc_members()?;
		let points = bonded_pool.try_bond_funds(&who, amount, BondType::Create)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3685-3696)
```rust
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

**File:** substrate/frame/nomination-pools/src/tests.rs (L256-289)
```rust
	fn ok_to_join_with_works() {
		ExtBuilder::default().build_and_execute(|| {
			let pool = BondedPool::<Runtime> {
				id: 123,
				inner: BondedPoolInner {
					commission: Commission::default(),
					member_counter: 1,
					points: 100,
					roles: DEFAULT_ROLES,
					state: PoolState::Open,
				},
			};

			let max_points_to_balance: u128 =
				<<Runtime as Config>::MaxPointsToBalance as Get<u8>>::get().into();

			// Simulate a 100% slashed pool
			set_pool_balance(pool.bonded_account(), 0);
			assert_noop!(pool.ok_to_join(), Error::<Runtime>::OverflowRisk);

			// Simulate a slashed pool at `MaxPointsToBalance` + 1 slashed pool
			set_pool_balance(pool.bonded_account(), max_points_to_balance.saturating_add(1));
			assert_ok!(pool.ok_to_join());

			// Simulate a slashed pool at `MaxPointsToBalance`
			set_pool_balance(pool.bonded_account(), max_points_to_balance);
			assert_noop!(pool.ok_to_join(), Error::<Runtime>::OverflowRisk);

			set_pool_balance(pool.bonded_account(), Balance::MAX / max_points_to_balance);

			// and a sanity check
			set_pool_balance(pool.bonded_account(), Balance::MAX / max_points_to_balance - 1);
			assert_ok!(pool.ok_to_join());
		});
```

**File:** substrate/frame/asset-conversion/src/lib.rs (L874-877)
```rust
			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
```
