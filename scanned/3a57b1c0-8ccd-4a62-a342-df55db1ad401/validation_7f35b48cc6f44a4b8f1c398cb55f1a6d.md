### Title
`RewardPool::update_records` can advance `last_recorded_total_payouts` without advancing `last_recorded_reward_counter`, permanently burning small reward deposits - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`RewardPool::update_records` in the nomination-pools pallet exhibits the exact bug class described in the external report: it can advance the "lastBalance" accounting value (`last_recorded_total_payouts`) without advancing the reward "index" (`last_recorded_reward_counter`) when the per-point reward amount rounds down to zero in `FixedU128` arithmetic. This causes real, non-zero reward balances that entered the reward account to be silently written off and become permanently unclaimable by any pool member.

### Finding Description
`RewardPool::current_reward_counter` computes the newly accrued, unclaimed payout balance and converts it into a per-point delta using `T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)`: [1](#0-0) 

`checked_from_rational` truncates (rounds toward zero) when the ratio is smaller than the fixed-point accuracy (`1 / 10^18` for `FixedU128`). This is demonstrably true in this codebase's own arithmetic tests, e.g. `checked_from_rational(1, accuracy + 1) == 0`: [2](#0-1) 

So whenever `new_pending_rewards / bonded_points < 10^-18`, the returned `current_reward_counter` is unchanged from `last_recorded_reward_counter` (adds effectively `0`) — this is the direct analog of `accrued.divDown(totalShares) == 0` in the reported LoopFi bug.

The critical flaw is in `update_records`, which is called after computing `current_reward_counter`: [3](#0-2) 

Regardless of whether `current_reward_counter` actually advanced, `update_records` unconditionally recomputes `last_recorded_total_payouts` from the pool's *current on-chain balance* (`balance.checked_add(total_rewards_claimed + total_commission_claimed)`), and stores the max of the old and new value. Since `balance` already includes any freshly-deposited (but un-recorded, because it rounded to zero in the reward counter) reward funds, `last_recorded_total_payouts` jumps forward to absorb that balance — exactly like `lastBalance` advancing in the LoopFi report while `index` does not. On the very next call, `current_payout_balance = balance + claimed - last_recorded_total_payouts` will no longer include the rounded-away increment, because it has already been "recorded" as paid out even though no reward counter increase, and therefore no member payout, ever occurred for it.

`update_records` is invoked from numerous public-facing extrinsics whenever bonded points or commission change — i.e., on `bond_extra`, `join`, `unbond`, `set_commission`, etc. — meaning any unprivileged member action (their own `join`/`bond`/`unbond`, or another member's actions that trigger a pool-wide update) can trigger this record advance and permanently seal off a small reward increment.

### Impact Explanation
This causes a genuine, permanent loss of pool member funds: reward tokens that were legitimately transferred into the pool's reward account become stuck and unclaimable by any member, because the accounting invariant (`last_recorded_total_payouts` tracks exactly what has been made claimable) is violated. Over the life of a large pool (many points, e.g. billions from `MaxPointsToBalance` scaling or heavy staking with `1e18`-scale point-to-balance ratios) with frequent small reward inflows (staking payouts happen every era, and `update_records` is triggered on every `bond`/`unbond`/`join`), this quietly and cumulatively "burns" reward balance — a chain-wide, protocol-level fund-loss bug affecting all delegators in the pool, not just an attacker's own funds.

### Likelihood Explanation
Reaching the rounding-to-zero branch requires `new_pending_rewards / bonded_points < 10^-18`, which can occur naturally with large, popular pools (billions of points) receiving comparatively small per-era reward increments, or repeated triggering of `update_records` in short succession by ordinary member activity (join/bond/unbond), similar to the "malicious user keeps calling `getRewards()`" aggravation noted by the original report's judge. No governance, admin, validator, or off-chain privileged actor is required — any member's normal, permissionless bond/unbond/join operation triggers `update_records` and can lock in the loss.

### Recommendation
Mirror the LoopFi fix: only advance `last_recorded_total_payouts` by the amount that was actually converted into `new_pending_rewards`/`new_pending_commission` and reflected in `last_recorded_reward_counter`/`total_commission_pending`, rather than snapping it to the raw current balance. Concretely, compute the delta balance corresponding to `new_pending_rewards.saturating_add(new_pending_commission)` and add only that delta to `last_recorded_total_payouts`, leaving any un-recorded remainder (the rounding dust) in the "unaccounted" balance so it is included in the next period's `current_payout_balance` calculation instead of being discarded.

### Proof of Concept
1. Create a pool with a very large number of bonded points (e.g. via many joins or a high `min_bond`/`MaxPointsToBalance` ratio), such that `bonded_points` is on the order of `10^18` or larger relative to per-era reward increments.
2. Transfer a small reward amount `R` into the reward account such that `R / bonded_points < 10^-18` (i.e., `R * 10^18 < bonded_points`).
3. Trigger any operation that calls `update_records` (e.g., a member calls `join` or `bond_extra` with a trivial amount, or `unbond`), invoking `RewardPool::current_reward_counter` → `checked_from_rational` rounding to add `0`, per [4](#0-3) .
4. Observe that `last_recorded_reward_counter` is unchanged, but `last_recorded_total_payouts` is bumped to include `R` (via [5](#0-4) ).
5. Subsequently, no member's `pending_rewards` (computed from the unchanged reward counter) reflects `R`, and any later `current_payout_balance` calculation permanently excludes `R` because it is now baked into `last_recorded_total_payouts`. The reward amount `R` is stuck in the reward account forever, unclaimable by any member — the existing `try-state` check `pending_rewards_lt_leftover_bal` at [6](#0-5)  only warns about excess/dust, it does not prevent or recover the loss.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1408-1446)
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

		// Store the reward counter at the time of this update. This is used in subsequent calls to
		// `current_reward_counter`, whereby newly pending rewards (in points) are added to this
		// value.
		self.last_recorded_reward_counter = current_reward_counter;

		// Add any new pending commission that has been calculated from `current_reward_counter` to
		// determine the total pending commission at the time of this update.
		self.total_commission_pending =
			self.total_commission_pending.saturating_add(new_pending_commission);

		// Total payouts are essentially the entire historical balance of the reward pool, equating
		// to the current balance + the total rewards that have left the pool + the total commission
		// that has left the pool.
		let last_recorded_total_payouts = balance
			.checked_add(&self.total_rewards_claimed.saturating_add(self.total_commission_claimed))
			.ok_or(Error::<T>::OverflowRisk)?;

		// Store the total payouts at the time of this update.
		//
		// An increase in ED could cause `last_recorded_total_payouts` to decrease but we should not
		// allow that to happen since an already paid out reward cannot decrease. The reward account
		// might go in deficit temporarily in this exceptional case but it will be corrected once
		// new rewards are added to the pool.
		self.last_recorded_total_payouts =
			self.last_recorded_total_payouts.max(last_recorded_total_payouts);

		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1462-1511)
```rust
		let current_payout_balance = balance
			.saturating_add(self.total_rewards_claimed)
			.saturating_add(self.total_commission_claimed)
			.saturating_sub(self.last_recorded_total_payouts);

		// Split the `current_payout_balance` into claimable rewards and claimable commission
		// according to the current commission rate.
		let new_pending_commission = commission * current_payout_balance;
		let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);

		// * accuracy notes regarding the multiplication in `checked_from_rational`:
		// `current_payout_balance` is a subset of the total_issuance at the very worse.
		// `bonded_points` are similarly, in a non-slashed pool, have the same granularity as
		// balance, and are thus below within the range of total_issuance. In the worse case
		// scenario, for `saturating_from_rational`, we have:
		//
		// dot_total_issuance * 10^18 / `minJoinBond`
		//
		// assuming `MinJoinBond == ED`
		//
		// dot_total_issuance * 10^18 / 10^10 = dot_total_issuance * 10^8
		//
		// which, with the current numbers, is a miniscule fraction of the u128 capacity.
		//
		// Thus, adding two values of type reward counter should be safe for ages in a chain like
		// Polkadot. The important note here is that `reward_pool.last_recorded_reward_counter` only
		// ever accumulates, but its semantics imply that it is less than total_issuance, when
		// represented as `FixedU128`, which means it is less than `total_issuance * 10^18`.
		//
		// * accuracy notes regarding `checked_from_rational` collapsing to zero, meaning that no
		//   reward can be claimed:
		//
		// largest `bonded_points`, such that the reward counter is non-zero, with `FixedU128` will
		// be when the payout is being computed. This essentially means `payout/bonded_points` needs
		// to be more than 1/1^18. Thus, assuming that `bonded_points` will always be less than `10
		// * dot_total_issuance`, if the reward_counter is the smallest possible value, the value of
		//   the
		// reward being calculated is:
		//
		// x / 10^20 = 1/ 10^18
		//
		// x = 100
		//
		// which is basically 10^-8 DOTs. See `smallest_claimable_reward` for an example of this.
		let current_reward_counter =
			T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)
				.and_then(|ref r| self.last_recorded_reward_counter.checked_add(r))
				.ok_or(Error::<T>::OverflowRisk)?;

		Ok((current_reward_counter, new_pending_commission))
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3999-4014)
```rust
			// the sum of the pending rewards must be less than the leftover balance. Since the
			// reward math rounds down, we might accumulate some dust here.
			let pending_rewards_lt_leftover_bal = RewardPool::<T>::current_balance(id) >=
				pools_members_pending_rewards.get(&id).copied().unwrap_or_default();

			// If this happens, this is most likely due to an old bug and not a recent code change.
			// We warn about this in try-runtime checks but do not panic.
			if !pending_rewards_lt_leftover_bal {
				log!(
					warn,
					"pool {:?}, sum pending rewards = {:?}, remaining balance = {:?}",
					id,
					pools_members_pending_rewards.get(&id),
					RewardPool::<T>::current_balance(id)
				);
			}
```

**File:** substrate/primitives/arithmetic/src/fixed_point.rs (L1606-1611)
```rust
				let a = $name::checked_from_rational(1, accuracy).unwrap();
				assert_eq!(a.into_inner(), 1);

				let a = $name::checked_from_rational(1, accuracy + 1).unwrap();
				assert_eq!(a.into_inner(), 0);
			}
```
