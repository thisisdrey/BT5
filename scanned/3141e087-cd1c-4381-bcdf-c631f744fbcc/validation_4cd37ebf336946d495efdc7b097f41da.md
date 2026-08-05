Based on my investigation, I found a solid local analog in the nomination-pools pallet's bonding logic — the same unchecked-shares-minted pattern as the external report.

### Title
Unchecked Zero-Point Issuance in `try_bond_funds` Can Silently Lock Bonded Funds With No Pool Credit - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`BondedPool::try_bond_funds` stakes/transfers a member's funds into the pool and issues pool "points" (the pools equivalent of vault shares) via `self.issue(amount)`, but never validates that the resulting `points_issued` is non-zero before committing the bond and updating `TotalValueLocked`. This mirrors the external report's core flaw: a deposit-like operation that trusts the share/point-minting step blindly, without checking that the depositor actually received a non-zero credit for the value they contributed.

### Finding Description
`try_bond_funds` computes points via `self.issue(amount)`, which internally calls `balance_to_point(bonded_balance, self.points, new_funds)` [1](#0-0) . This is invoked unconditionally from `try_bond_funds`, and the returned `points_issued` is passed straight back to the caller with no floor/zero check [2](#0-1) .

The point/balance conversion math is a truncating integer ratio (`u256(current_balance) * u256(points) / u256(current_points)`), as shown by the mirror function `point_to_balance`, which explicitly documents this division and can floor small inputs to zero [3](#0-2) . `balance_to_point` performs the analogous `new_funds * current_points / bonded_balance` computation, so when a pool has accumulated a large points:balance ratio (e.g. after repeated dilution/slashing events pushed the ratio far from 1:1, or simply a very large existing bonded balance relative to a small `amount`), a nonzero `amount` can be converted to `0` points due to integer truncation.

This zero-point outcome is never checked in the call path:
- `do_bond_extra` uses `try_bond_funds` to obtain `points_issued` for both `BondExtra::FreeBalance` and `BondExtra::Rewards`, then unconditionally adds it to `member.points` and staking bond proceeds regardless of whether `points_issued` is zero [4](#0-3) .
- `do_create` similarly consumes `try_bond_funds`'s return value without validating it is non-zero [5](#0-4) .
- `join` (the public, unprivileged extrinsic) does the same: `bonded_pool.try_bond_funds(&who, amount, BondType::Extra)?` result is stored directly as the member's `points` with no zero check [6](#0-5) .

Contrast this with `pallet_asset_conversion::do_add_liquidity`, which explicitly guards against this exact class of bug by checking `ensure!(lp_token_amount > T::MintMinLiquidity::get(), Error::<T>::InsufficientLiquidityMinted)` before minting LP tokens [7](#0-6) . Nomination-pools has no equivalent guard for pool points.

### Impact Explanation
If `join`/`bond_extra`/`bond_extra_other`/`create` issue `0` points for a nonzero staked amount, the caller's funds are transferred to the pool's bonded account and staked (increasing `TotalValueLocked` and the pool's actual stake), but the depositor's `PoolMember.points` (or the newly created member's points) do not increase, or increase by zero. The depositor's claim on the pool's bonded balance is permanently unrepresented — their contributed funds are effectively donated to all other pool members pro-rata, and the depositor cannot unbond/withdraw the value they just staked because unbonding is based on points, not raw balance. This is a fund-lock/loss condition reachable by an ordinary unprivileged account with no admin or governance involvement, matching the "permanent user-fund lock" and "value not conserved / not settled to rightful beneficiary" impact classes in scope.

### Likelihood Explanation
This requires the pool's points:balance ratio to have drifted far enough (via slashing or extreme pool growth) that a legitimate small bond amount truncates to zero points under the `u256` integer division in `balance_to_point`. `BondedPool::ok_to_join` does check for `OverflowRisk` when the ratio is too extreme in the *opposite* direction (points vastly exceeding balance) as shown in `ok_to_join_with_works` tests [8](#0-7) , but that guard bounds the ratio only up to `MaxPointsToBalance`, and does not protect the `bond_extra`/`join` paths against a *small enough* `amount` producing a truncated-to-zero result within an otherwise "valid" ratio. This makes the likelihood moderate rather than trivial — it depends on specific pool state (ratio and bond size combination) rather than being exploitable in the default 1:1 fresh-pool case.

### Recommendation
Add an explicit check in `try_bond_funds` (or immediately after calling `self.issue(amount)`) that rejects the bond when `points_issued.is_zero()` and `amount` is nonzero, e.g.:
```rust
let points_issued = self.issue(amount);
ensure!(!points_issued.is_zero() || amount.is_zero(), Error::<T>::InsufficientBond);
```
This mirrors the `InsufficientLiquidityMinted` guard already present in `pallet_asset_conversion::do_add_liquidity`.

### Proof of Concept
1. A pool exists where, due to accumulated slashing history or organic growth, `bonded_balance / points` yields a large ratio (still within `MaxPointsToBalance` bounds enforced by `ok_to_join`).
2. An account calls `Pools::join(origin, amount, pool_id)` (or `bond_extra`) with an `amount` small enough that `balance_to_point(bonded_balance, points, amount)` truncates to `0` under integer division.
3. `try_bond_funds` stakes/transfers `amount` into the pool's bonded account (`TotalValueLocked` and the pool's actual staked balance increase), but returns `points_issued == 0`.
4. `PoolMembers::<T>::insert` stores the member with `points: 0`, so the member has no claim on the funds they just contributed — the deposit is effectively lost to the depositor and diluted among existing members. [2](#0-1) [6](#0-5)

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L874-877)
```rust
			ensure!(
				lp_token_amount > T::MintMinLiquidity::get(),
				Error::<T>::InsufficientLiquidityMinted
			);
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
