## Analysis

The Audius bug's core broken invariant is: **a value that determines how rewards are split between an operator and its delegators can be changed with no delay, and the change takes effect immediately on the very next payout calculation**, letting the operator front-run its own delegators' claims.

The direct on-chain analog is `pallet-nomination-pools`, where the pool's commission role (`root`/whoever holds `can_manage_commission`) plays the same part as an Audius "service provider," and pool members play the same part as delegators.

### The corrupted value
`BondedPoolInner::commission.current` (a `(Perbill, AccountId)` pair), mutated via the public extrinsic `set_commission`, and consumed live by `do_reward_payout`/`do_claim_commission` at claim time.

### Why the guard doesn't help
`try_update_current` only throttles a commission increase if a `change_rate` has been configured: [1](#0-0) 

But `change_rate` is **optional** and defaults to `None`. `throttling()` short-circuits to `false` whenever `self.change_rate` is `None`: [2](#0-1) 

So unless the pool's commission-manager has proactively called `set_commission_change_rate`, `set_commission` can jump the commission from any value straight to 100% (or up to `max`, which also defaults to `None`) in a single block, with no timelock — exactly the missing safeguard the Audius report calls out for `deployerCut`.

Unlike `pallet-staking`, which snapshots validator commission once per era into `ErasValidatorPrefs` at `store_stakers_info` (so a live `validate()` commission change cannot retroactively affect an already-planned era's payout): [3](#0-2) 

`pallet-nomination-pools` has no such snapshot. `do_reward_payout` computes `current_reward_counter` using the pool's **live** `bonded_pool.commission.current()` at the exact moment of the claim: [4](#0-3) 

And the commission split itself is calculated at that same live rate: [5](#0-4) 

A test in the codebase already demonstrates the mechanic (just without an attacker framing): setting commission to a higher value causes a member's pending reward to be reduced/zeroed based on whatever the commission is at the moment `claim_payout`/`current_reward_counter` runs: [6](#0-5) 

### Title
Nomination pool commission can be instantly raised to 100% with no timelock, letting the pool operator siphon members' pending rewards before they claim - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Pallet::set_commission` lets the pool's commission-manager change `BondedPoolInner.commission.current` at any time. The change-rate throttle (`CommissionChangeRate`) that would normally bound/delay increases is opt-in and defaults to `None`, in which case `Commission::throttling` always returns `false`. Because `do_reward_payout` and `do_claim_commission` compute rewards using the commission rate live, at the block the claim executes, an operator can raise commission to 100% (or to `max`, itself optional) immediately before members claim, redirecting the entire pending reward pot to itself instead of the members — the same broken invariant as the Audius `deployerCut`/`claimRewards` race described in the source report.

### Finding Description
`try_update_current` is the only gate on commission changes: [7](#0-6) 
It rejects a change only if `throttling()` returns true, and `throttling()` is unconditionally `false` when no `change_rate` was ever configured for the pool. Since `set_commission_change_rate` is a separate, optional extrinsic that operators are not forced to call, most pools can have their commission changed instantaneously, up to `GlobalMaxCommission` (which itself may be unset) or the pool's own `max` (also optional).

The reward-splitting logic then uses whatever `commission.current()` is *at call time*: [5](#0-4) 
There is no era-level or block-level snapshot of the commission rate that applies to already-accrued-but-unclaimed rewards, unlike `pallet-staking`'s `ErasValidatorPrefs`, which fixes commission for an entire era at election time. Consequently, every unclaimed member reward in a nomination pool is exposed to whatever commission is active the instant `claim_payout` (or `claim_payout_other`) executes — regardless of when those rewards actually accrued.

### Impact Explanation
A pool operator (or anyone who holds `can_manage_commission` for the pool) can extract funds that rightfully belong to pool members: raise commission to 100% right before a member's claim executes, drain the accrued reward balance via `do_claim_commission`, then lower commission back down. This is a value-conservation/wrong-beneficiary violation on real staked funds, matching the "theft ... duplicate settlement or payout" and "conserve value and settle exactly once to the rightful beneficiary" impact classes in scope.

### Likelihood Explanation
No privileged chain-level actor is required — only the pool's own commission-manager role, which is the direct analog of the Audius "service provider." The default configuration (no `change_rate`, no `max`) is the out-of-the-box behavior for any newly created pool unless the operator explicitly opts into throttling, making this a realistic default-state exposure for members joining pools that have not configured a change rate.

### Recommendation
Enforce a mandatory minimum change-rate/timelock for commission increases by default (e.g., require `change_rate` to be set at pool creation, or apply a hard-coded minimum delay/`max_increase` when none is configured), and/or snapshot the commission rate applied to already-accrued-but-unclaimed reward balances at the time they were earned, rather than at claim time — mirroring the era-snapshot approach used by `pallet-staking`'s `ErasValidatorPrefs`.

### Proof of Concept
1. Pool `root` creates a pool with default `Commission { change_rate: None, max: None, .. }`.
2. Members join and rewards accrue in the pool's reward account over time (`deposit_rewards`/staking rewards).
3. Immediately before a member calls `claim_payout`, `root` calls:
   `Pools::set_commission(RuntimeOrigin::signed(root), pool_id, Some((Perbill::from_percent(100), root)))`
   — this succeeds instantly because `throttling()` returns `false` with no `change_rate` set (see `substrate/frame/nomination-pools/src/lib.rs:802-816,838-841`).
4. The member's `claim_payout` now computes `current_reward_counter` with `commission = 100%`; `new_pending_rewards = current_payout_balance - commission*current_payout_balance = 0`, so `do_reward_payout` returns `0` (see `substrate/frame/nomination-pools/src/lib.rs:3539-3550,1462-1471`).
5. `root` then calls `claim_commission`, receiving the entire accrued reward balance that should have been split with members, exactly reproducing the Audius `deployerCut`-before-`claimRewards` scenario.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L802-816)
```rust
	fn throttling(&self, to: &Perbill) -> bool {
		if let Some(t) = self.change_rate.as_ref() {
			let commission_as_percent =
				self.current.as_ref().map(|(x, _)| *x).unwrap_or(Perbill::zero());

			// do not throttle if `to` is the same or a decrease in commission.
			if *to <= commission_as_percent {
				return false;
			}
			// Test for `max_increase` throttling.
			//
			// Throttled if the attempted increase in commission is greater than `max_increase`.
			if (*to).saturating_sub(commission_as_percent) > t.max_increase {
				return true;
			}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L838-841)
```rust
			);
		}
		false
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L857-879)
```rust
	fn try_update_current(&mut self, current: &Option<(Perbill, T::AccountId)>) -> DispatchResult {
		self.current = match current {
			None => None,
			Some((commission, payee)) => {
				ensure!(!self.throttling(commission), Error::<T>::CommissionChangeThrottled);
				ensure!(
					commission <= &GlobalMaxCommission::<T>::get().unwrap_or(Bounded::max_value()),
					Error::<T>::CommissionExceedsGlobalMaximum
				);
				ensure!(
					self.max.map_or(true, |m| commission <= &m),
					Error::<T>::CommissionExceedsMaximum
				);
				if commission.is_zero() {
					None
				} else {
					Some((*commission, payee.clone()))
				}
			},
		};
		self.register_update();
		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1462-1471)
```rust
		let current_payout_balance = balance
			.saturating_add(self.total_rewards_claimed)
			.saturating_add(self.total_commission_claimed)
			.saturating_sub(self.last_recorded_total_payouts);

		// Split the `current_payout_balance` into claimable rewards and claimable commission
		// according to the current commission rate.
		let new_pending_commission = commission * current_payout_balance;
		let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3539-3550)
```rust
		let (current_reward_counter, _) = reward_pool.current_reward_counter(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;

		// Determine the pending rewards. In scenarios where commission is 100%, `pending_rewards`
		// will be zero.
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}
```

**File:** substrate/frame/staking/src/pallet/impls.rs (L725-729)
```rust
		// Collect the pref of all winners.
		for stash in &elected_stashes {
			let pref = Validators::<T>::get(stash);
			<ErasValidatorPrefs<T>>::insert(&new_planned_era, stash, pref);
		}
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L6333-6398)
```rust
	#[test]
	fn commission_reward_counter_works_one_member() {
		ExtBuilder::default().build_and_execute(|| {
			let pool_id = 1;
			let root = 900;
			let member = 10;

			// Set the pool commission to 10% to test commission shares. Pool is topped up 40 points
			// and `member` immediately claims their pending rewards. Reward pool should still have
			// 10% share.

			// Given:
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(root),
				1,
				Some((Perbill::from_percent(10), root)),
			));
			deposit_rewards(40);

			// When:
			assert_ok!(Pools::claim_payout(RuntimeOrigin::signed(10)));

			// Then:
			assert_eq!(RewardPool::<Runtime>::current_balance(pool_id), 4);

			// Set pool commission to 20% and repeat the same process.

			// When:
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(root),
				1,
				Some((Perbill::from_percent(20), root)),
			));

			// Then:
			assert_eq!(
				RewardPools::<Runtime>::get(pool_id).unwrap(),
				RewardPool {
					last_recorded_reward_counter: FixedU128::from_float(3.6),
					last_recorded_total_payouts: 40,
					total_rewards_claimed: 36,
					total_commission_pending: 4,
					total_commission_claimed: 0
				}
			);

			// The current reward counter should yield the correct pending rewards of zero.

			// Given:
			let (current_reward_counter, _) = RewardPools::<Runtime>::get(pool_id)
				.unwrap()
				.current_reward_counter(
					pool_id,
					BondedPools::<Runtime>::get(pool_id).unwrap().points,
					Perbill::from_percent(20),
				)
				.unwrap();

			// Then:
			assert_eq!(
				PoolMembers::<Runtime>::get(member)
					.unwrap()
					.pending_rewards(current_reward_counter)
					.unwrap(),
				0
			);
```
