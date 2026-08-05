Based on my analysis of the code, I found that the claim is accurate and confirmed by the actual source.

The `current_reward_counter` function computes `T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)` without any zero-check on `bonded_points`. [1](#0-0) 

I verified the underlying arithmetic primitive: `FixedPointNumber::checked_from_rational` unconditionally returns `None` whenever the denominator is zero, regardless of the numerator's value (including when the numerator is also zero). [2](#0-1) 

This is confirmed by the test suite for this primitive, which explicitly documents `checked_from_rational(1, 0) => None`. [3](#0-2) 

This means that when `bonded_points == 0`, any call into `update_records` will propagate `Error::<T>::OverflowRisk`, since `update_records` calls `current_reward_counter` and immediately propagates its error with `?`. [4](#0-3) 

The sibling helpers `balance_to_point` and `point_to_balance` do explicitly guard against zero `current_points`/`current_balance` before dividing, confirming the pallet authors are aware zero is a reachable state for pool point/balance totals, but no analogous guard exists in `current_reward_counter`. [5](#0-4) [6](#0-5) 

The call sites cited by the claim (`join`, `set_commission`, `set_commission_max`, `do_claim_commission`) all pass `bonded_pool.points` straight into `update_records` without any zero check beforehand — I was unable to complete a final verification within the remaining budget of whether any of these entry points is actually gated by a `PoolState` restriction that would prevent them from being called once `bonded_pool.points` reaches exactly zero (e.g., whether `set_commission`/`set_commission_max`/`claim_commission` remain callable in a `Destroying` state pool with zero points, or whether zero points would already imply the pool's reward account balance is also fully flushed/zero at that point). This is the crux of whether the bug is actually reachable in practice (a "realistic ordering" where `new_pending_rewards != 0` while `bonded_points == 0`), and I could not fully trace all call sites and state-transition guards (e.g. `ok_to_join`, `ok_to_unbond_with`, or any check like `!bonded_pool.points.is_zero()`) before running out of iterations.

Given the code-level mechanism (unguarded division-by-zero via `checked_from_rational` causing `OverflowRisk`) is verified as real and matches the claim precisely, but I could not fully confirm the reachability/exploitability conditions (specifically whether commission-claim/set-commission calls remain permitted on a zero-points pool with non-zero unflushed reward balance, and whether such a state is reachable through purely permissionless member actions), I cannot assert full confidence on the Impact/Likelihood sections independently. However, the finding as submitted, with its exact citations, code paths, and root-cause description, does map to an existing, unguarded code path in the repository that matches the pallet's actual behavior.

Audit Report

## Title
`RewardPool::current_reward_counter` reverts when `bonded_pool.points == 0`, permanently locking un-claimed pool reward/commission funds - (File: substrate/frame/nomination-pools/src/lib.rs)

## Summary
`RewardPool::current_reward_counter` divides `new_pending_rewards` by `bonded_points` via `T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)` with no zero-guard on `bonded_points`, unlike the sibling helpers `balance_to_point`/`point_to_balance` which explicitly special-case a zero denominator. Since `checked_from_rational` unconditionally returns `None` when the denominator is zero, any call path that must run `update_records` (`join`, `set_commission`, `set_commission_max`, `do_claim_commission`) will revert with `Error::<T>::OverflowRisk` once `bonded_pool.points` reaches zero, potentially permanently locking any un-flushed reward-account balance.

## Finding Description
`current_reward_counter` computes `T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)` and converts a `None` result directly into `Error::<T>::OverflowRisk` via `.ok_or(...)`. [1](#0-0) 
`checked_from_rational` in the arithmetic primitives crate returns `None` whenever the denominator equals zero, independent of the numerator value. [2](#0-1) 
`update_records`, which is a mandatory precondition invoked from `set_commission`, `set_commission_max`, `do_claim_commission`, and `join`, forwards `bonded_pool.points` straight into `current_reward_counter` with the `?` operator, so any `OverflowRisk` error aborts the calling extrinsic. [4](#0-3) 
By contrast, `point_to_balance`/`balance_to_point` explicitly branch on `current_points.is_zero()` / `current_balance.is_zero()` before performing any division, demonstrating that the pallet's design already accounts for the zero-points state elsewhere but omits the same guard in `current_reward_counter`. [5](#0-4) 

## Impact Explanation
If reachable, this would constitute a permanent lock of un-flushed reward-account balance (commission and/or dust), since any mutating call on the reward-pool records (`set_commission`, `set_commission_max`, `claim_commission`) is gated by the now-permanently-failing `update_records`/`current_reward_counter`, matching the "permanent user-fund lock" category in the impact gate — provided the zero-points/non-zero-unflushed-balance state is actually reachable and these calls remain permitted in that state.

## Likelihood Explanation
The precondition requires `bonded_pool.points` to reach exactly zero while the reward account retains an unflushed, non-zero balance, and further requires that `set_commission`/`set_commission_max`/`claim_commission` still be dispatchable on that pool at that point. I was not able to fully verify, within available tool budget, whether pool-state guards (e.g., checks tied to `PoolState`/`Destroying` transitions or other `ensure!` conditions on these dispatchables) prevent this scenario from arising through purely permissionless member actions, so the likelihood claim in the original submission (fully permissionless, no privileged action required) is only partially verified against the current repository state.

## Recommendation
Mirror the zero-denominator handling used in `balance_to_point`/`point_to_balance` inside `RewardPool::current_reward_counter` by explicitly special-casing `bonded_points.is_zero()` rather than letting `checked_from_rational` collapse to `Error::<T>::OverflowRisk`, and audit whether `set_commission`, `set_commission_max`, and `do_claim_commission` should additionally guard against a zero-points bonded pool before calling `update_records`.

## Proof of Concept
1. Set non-zero commission on a pool via `set_commission`.
2. Deposit rewards into the pool's reward account so that `current_payout_balance` in `current_reward_counter` becomes non-zero and unflushed (not yet folded into `last_recorded_total_payouts`).
3. Drive `bonded_pool.points` to exactly zero through ordinary `unbond` calls.
4. Call `set_commission`, `set_commission_max`, or `claim_commission` and observe `update_records` → `current_reward_counter` → `checked_from_rational(new_pending_rewards, 0)` → `None` → `Error::<T>::OverflowRisk`.
(Note: step 3's reachability while these dispatchables remain callable was not independently confirmed against pool-state guards in this review.)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1408-1417)
```rust
	fn update_records(
		&mut self,
		id: PoolId,
		bonded_points: BalanceOf<T>,
		commission: Perbill,
	) -> Result<(), Error<T>> {
		let balance = Self::current_balance(id);

		let (current_reward_counter, new_pending_commission) =
			self.current_reward_counter(id, bonded_points, commission)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1506-1509)
```rust
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3482-3498)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3509-3513)
```rust
		let balance = T::U256ToBalance::convert;
		if current_balance.is_zero() || current_points.is_zero() || points.is_zero() {
			// There is nothing to unbond
			return Zero::zero();
		}
```

**File:** substrate/primitives/arithmetic/src/fixed_point.rs (L179-199)
```rust
	fn checked_from_rational<N: FixedPointOperand, D: FixedPointOperand>(
		n: N,
		d: D,
	) -> Option<Self> {
		if d == D::zero() {
			return None;
		}

		let n: I129 = n.into();
		let d: I129 = d.into();
		let negative = n.negative != d.negative;

		multiply_by_rational_with_rounding(
			n.value,
			Self::DIV.unique_saturated_into(),
			d.value,
			Rounding::from_signed(SignedRounding::Minor, negative),
		)
		.and_then(|value| from_i129(I129 { value, negative }))
		.map(Self::from_inner)
	}
```

**File:** substrate/primitives/arithmetic/src/fixed_point.rs (L1552-1554)
```rust
				// Divide by zero => None.
				let a = $name::checked_from_rational(1, 0);
				assert_eq!(a, None);
```
