### Title
Missing slippage protection in `pallet-nomination-pools` `join`/`bond_extra`/`unbond` — no minimum-points/minimum-balance guard on points↔balance conversion - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
The `OmoRouter`/`OmoVault` report's core broken invariant is: a deposit/mint/redeem call that converts one unit of value into another via an on-chain, time-varying exchange rate accepts *any* result, with no caller-supplied bound to reject an unfavorable outcome. `pallet-asset-conversion` and the newly added `pallet-psm` both already implement this pattern correctly (`amount_out_min`/`amount_in_max`, `max_fee`). `pallet-nomination-pools`, however, implements the exact same class of value-conversion entrypoints — `join`, `bond_extra`, and `unbond` — using the pool's `points`⇄`balance` ratio, but exposes **no minimum-points-out or minimum-balance-out parameter** at all.

### Finding Description
`join` and `bond_extra` convert a member's deposited balance into pool `points` using `Pallet::<T>::balance_to_point`, and `unbond` converts points back into balance using `point_to_balance`: [1](#0-0) [2](#0-1) 

`join` calls `bonded_pool.try_bond_funds(&who, amount, BondType::Extra)` and inserts `PoolMember { points: points_issued, .. }` with no check that `points_issued` meets any member-specified minimum: [3](#0-2) 

`bond_extra` follows the same pattern, converting `FreeBalance`/`Rewards` into points via `try_bond_funds` with no minimum-points parameter: [4](#0-3) 

The ratio (`bonded_pool.points / active_stake`) is **not static between submission and inclusion**: it moves due to slashing (`do_slash`, applied lazily and reflected immediately in `active_stake`/`total_balance()`), reward-triggered `do_reward_payout` calls executed atomically inside `bond_extra`, and other members' `join`/`unbond`/`withdraw_unbonded` in the same or an earlier block. Documentation itself states the ratio changes over time due to slashing: [5](#0-4) 

Contrast this with `pallet-asset-conversion`, which treats exactly this scenario (rate drift between submission and execution) as requiring caller-specified protection via `amount_out_min`/`amount_in_max`, enforced with `Error::<T>::ProvidedMinimumNotSufficientForSwap` / `ProvidedMaximumNotSufficientForSwap`: [6](#0-5) [7](#0-6) 

And `pallet-psm`'s `mint`/`redeem` guard the fee-rate portion of the conversion with `max_fee`: [8](#0-7) [9](#0-8) 

`pallet-nomination-pools` has no analogous parameter on `join`, `bond_extra`, or `unbond`. `MinJoinBond`/`MinimumBondNotMet` only bound the *balance amount*, not the resulting points-per-balance rate, so they do nothing to protect against rate drift.

### Impact Explanation
An unprivileged user's `join`/`bond_extra`/`unbond` transaction can execute with a materially different points↔balance ratio than existed when they signed the transaction, if a slash lands (or another large event alters `active_stake`/`points`) between signing and inclusion. Because there is no minimum-output check, the extrinsic still succeeds and silently mints fewer points (on `join`/`bond_extra`) or unbonds less balance than expected (on `unbond`), causing direct, un-refundable value loss to the member — a real fund-loss impact under "Balances, assets, NFTs, staking, pools... must conserve value and settle exactly once to the rightful beneficiary and amount."

### Likelihood Explanation
Medium. This requires no malicious actor, admin, or governance action — it is a natural race between the ordinary passage of blocks/eras (slashing is intentionally lazy in this pallet) and normal user transaction inclusion. Given staking slashes and reward payouts are routine on live networks, and pool ratios change every time other members join/unbond, the window for an unfavorable rate to land between signing and execution is not contrived.

### Recommendation
Add optional minimum-output/maximum-input-equivalent parameters to `join`, `bond_extra`, and `unbond`, analogous to `amount_out_min`/`amount_in_max` in `pallet-asset-conversion`:
- `join(origin, amount, pool_id, min_points_issued: Option<BalanceOf<T>>)` — reject if `try_bond_funds` yields fewer points.
- `bond_extra(origin, extra, min_points_issued: Option<BalanceOf<T>>)` — same check.
- `unbond(origin, member_account, unbonding_points, min_balance_out: Option<BalanceOf<T>>)` — reject if the computed `balance_to_unbond` (via `dissolve`) is below the caller's bound.

### Proof of Concept
1. Alice observes pool 1 has `points = 1000`, `active_stake = 1000` (1:1 ratio) and signs `join(amount = 100, pool_id = 1)`, expecting ~100 points.
2. Before Alice's extrinsic is included, a slash lands on the pool's bonded account (lazily applied via `do_slash`), reducing `active_stake` to 500 while `points` remains 1000 (2 points per 1 balance ratio is broken the other way — balance halves).
3. Alice's `join` executes in the same or later block. `balance_to_point` now computes far fewer points for her 100 balance transferred (per the `(current_points/current_balance)*new_funds` formula in `substrate/frame/nomination-pools/src/lib.rs:3489-3497`), e.g., 200 points instead of the ~100 she expected, or in the reverse-ratio slash direction, significantly less claim than anticipated.
4. `join` has no way to reject this outcome — there is no `min_points_issued` parameter — so the call succeeds and Alice is stuck with a worse points allocation than she agreed to when signing, with no recourse.
5. The same lack of protection applies symmetrically to `unbond`, where a member requesting to unbond a fixed number of points can receive less balance than expected if the ratio shifts unfavorably before execution.

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2120-2174)
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

			Self::deposit_event(Event::<T>::Bonded {
				member: who,
				pool_id,
				bonded: amount,
				joined: true,
			});

			bonded_pool.put();
			RewardPools::<T>::insert(pool_id, reward_pool);

			Ok(())
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3652-3707)
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

		Self::deposit_event(Event::<T>::Bonded {
			member: member_account.clone(),
			pool_id: member.pool_id,
			bonded,
			joined: false,
		});
		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);

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

**File:** substrate/frame/asset-conversion/src/lib.rs (L1036-1050)
```rust
			ensure!(amount_out > Zero::zero(), Error::<T>::ZeroAmount);
			if let Some(amount_in_max) = amount_in_max {
				ensure!(amount_in_max > Zero::zero(), Error::<T>::ZeroAmount);
			}

			Self::validate_swap_path(&path)?;
			let path = Self::balance_path_from_amount_out(amount_out, path)?;

			let amount_in = path.first().map(|(_, a)| *a).ok_or(Error::<T>::InvalidPath)?;
			if let Some(amount_in_max) = amount_in_max {
				ensure!(
					amount_in <= amount_in_max,
					Error::<T>::ProvidedMaximumNotSufficientForSwap
				);
			}
```

**File:** substrate/frame/psm/src/lib.rs (L702-722)
```rust
		pub fn mint(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			external_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_minting(), Error::<T>::MintingStopped);

			let (ext_decimals, internal_decimals) =
				Self::ensure_decimals_match(&info, &internal_asset, &external_asset, &external)?;

			let internal_equivalent =
				Self::external_to_internal(external_amount, ext_decimals, internal_decimals)?;
			ensure!(!internal_equivalent.is_zero(), Error::<T>::AmountTooSmallAfterConversion);
			ensure!(internal_equivalent >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);
```

**File:** substrate/frame/psm/src/lib.rs (L811-833)
```rust
		pub fn redeem(
			origin: OriginFor<T>,
			internal_asset: T::AssetId,
			external_asset: T::AssetId,
			internal_amount: BalanceOf<T>,
			max_fee: Permill,
		) -> DispatchResult {
			let who = ensure_signed(origin)?;
			let info = Psm::<T>::get(&internal_asset).ok_or(Error::<T>::PsmNotFound)?;

			let external = ExternalAssets::<T>::get(&internal_asset, &external_asset)
				.ok_or(Error::<T>::UnsupportedAsset)?;
			ensure!(external.status.allows_redemption(), Error::<T>::AllSwapsStopped);

			let ext_decimals = external.decimals;
			let internal_decimals = info.internal_decimals;

			ensure!(internal_amount >= info.min_swap_amount, Error::<T>::BelowMinimumSwap);

			let fee_rate = RedemptionFee::<T>::get(&internal_asset, &external_asset);
			ensure!(fee_rate <= max_fee, Error::<T>::FeeTooHigh);
			let fee = fee_rate.mul_ceil(internal_amount);
			let internal_net = internal_amount.saturating_sub(fee);
```
