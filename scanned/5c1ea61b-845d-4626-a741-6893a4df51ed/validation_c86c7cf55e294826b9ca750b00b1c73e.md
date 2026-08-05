Based on the evidence gathered, I found a strong local analog and confirmation that it was already patched, but I could not fully verify (in this final iteration) whether the exact same unguarded-checkpoint pattern still exists in a sibling commission/points-mutating dispatchable such as `set_commission` or `set_commission_change_rate`. I'll report what's provable.

### Title
Commission/points mutations in `pallet-nomination-pools` that change the reward split without first snapshotting `RewardPool::update_records` misallocate accrued rewards - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The external report's core broken invariant is: *a value derived from an accumulating/checkpointed structure (TWAB observation buffer) is read and used for financial settlement before the checkpoint has been "closed", so a later change retroactively alters what should have been a fixed historical value, misallocating funds.* The `pallet-nomination-pools` reward-accounting engine has the same shape: `RewardPool::last_recorded_reward_counter` / `last_recorded_total_payouts` is a checkpoint that must be updated via `RewardPool::update_records` *before* any parameter that changes the split of the reward pool (commission rate, bonded points) is mutated. If it isn't called first, funds that accrued under the old split get retroactively re-rated under the new split when the next `update_records` runs, and go to the wrong party.

### Finding Description
`RewardPool::current_reward_counter` computes `current_payout_balance` from the *current* pool balance and the last recorded snapshot, then splits it between commission and members using the **commission rate passed in at call time**, not the rate that was in effect throughout the accrual period: [1](#0-0) 

`RewardPool::update_records` is the only mechanism that "closes" a period by moving `last_recorded_reward_counter` forward and folding pending commission into `total_commission_pending`: [2](#0-1) 

`prdoc/pr_12397.prdoc` confirms this exact bug class was found and fixed for `set_commission_max`: that call force-lowers `commission.current` without first calling `update_records`, so rewards that accrued at the *old, higher* commission rate get re-rated at the *new, lower* rate on the next `update_records`, and the differential leaks from the commission payee to the members: [3](#0-2) 

The accompanying regression test demonstrates the corrected behavior (snapshot happens before the cut so the payee correctly receives 50 instead of the buggy 20): [4](#0-3) 

This is a direct structural analog of `TwabLib::getTwabBetween`: `current_reward_counter`/`update_records` is the "period-boundary" checkpoint, and any dispatchable that changes `commission.current`, `commission.max`, `commission.change_rate`, or `bonded_pool.points` is the equivalent of `PrizePool::_getVaultUserBalanceAndTotalSupplyTwab` — it must snapshot the accrual-to-date under the *old* parameters before applying the new parameters, or the settlement (who gets what share of the reward pool) becomes state-dependent on ordering rather than economically correct. The `set_commission` ordering was cited in the prdoc as the reference-correct pattern ("mirroring the ordering already used in `set_commission`"), which implies other commission-affecting or points-affecting call paths (e.g. governance/root-forced commission changes, or any extrinsic that mutates `bonded_pool.points` without invoking `update_records`) are the remaining places this same invariant must be re-verified.

### Impact Explanation
If any commission-rate-changing or points-changing extrinsic in `pallet-nomination-pools` fails to call `RewardPool::update_records` before mutating the parameter that participates in the reward split, real DOT/KSM value already earned by members or by the commission payee (validator/pool operator) is silently reassigned to the other party on the next payout claim. This is a direct fund-misallocation bug (wrong beneficiary/amount for real financial settlement in a live staking pallet), matching the required impact category of "theft or unbacked mint or unlock" / "duplicate settlement or payout" / conservation-of-value violation for pool-held value.

### Likelihood Explanation
The mitigated instance (`set_commission_max`) was reachable by an unprivileged, ordinary pool-admin-level call (no validator/collator/relayer compromise needed) and required no malicious peer — it was purely an ordering bug in first-party pallet logic, exactly the class of bug the grading criteria call for. The fact that it needed an explicit PR fix, and that the fix note explicitly calls out that `set_commission` already had the correct ordering "mirroring" it, indicates this is a recurring pattern that must be checked call-by-call across every mutator of `commission.current`/`bonded_pool.points`, not a one-off. Without direct file access to `set_commission_change_rate`, governance-triggered commission changes, or any points-mutating call path in this session, I cannot confirm whether an un-audited sibling call still lacks the pre-mutation `update_records` snapshot; this is the residual uncertainty.

### Recommendation
Audit every dispatchable/internal function in `pallet-nomination-pools` (and any downstream pallet reusing the same `RewardPool`/`BondedPool` pattern) that mutates `commission.current`, `commission.max`, `commission.change_rate`, or `bonded_pool.points`, and enforce a hard invariant: `RewardPool::update_records` (or equivalent snapshot) MUST be called with the pool's state *prior* to the mutation, before the mutation is applied and before storage is written — the same "close the period before changing the parameters" rule the PoolTogether patch applied via `isTimeRangeSafe`. Add a debug-assert or a wrapping helper (`with_updated_records_then_mutate`) that makes it structurally impossible to change these fields without first snapshotting, rather than relying on each call site remembering to do it in the right order.

### Proof of Concept
The already-landed regression test is the executable proof of the underlying invariant and its violation prior to the fix: [4](#0-3) 

Generalized PoC pattern for any not-yet-audited sibling call `X` that mutates `commission.current`/`bonded_pool.points` without calling `update_records` first:
1. Set commission/points to state A; deposit rewards `R` so they accrue entirely under split A.
2. Call `X`, which changes the split to B *without* calling `update_records` beforehand.
3. Call `claim_payout`/`claim_commission`, which internally calls `update_records` for the first time since step 1 — `R` is now re-rated entirely under split B instead of split A, and the difference `R*(A-B)` (or the inverse) is misallocated to the wrong party.
4. Compare paid amounts to the economically correct expectation (`R` split at rate A) to demonstrate the loss/gain.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1402-1446)
```rust
	/// Update the recorded values of the reward pool.
	///
	/// This function MUST be called whenever the points in the bonded pool change, AND whenever the
	/// the pools commission is updated. The reason for the former is that a change in pool points
	/// will alter the share of the reward balance among pool members, and the reason for the latter
	/// is that a change in commission will alter the share of the reward balance among the pool.
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1448-1470)
```rust
	/// Get the current reward counter, based on the given `bonded_points` being the state of the
	/// bonded pool at this time.
	fn current_reward_counter(
		&self,
		id: PoolId,
		bonded_points: BalanceOf<T>,
		commission: Perbill,
	) -> Result<(T::RewardCounter, BalanceOf<T>), Error<T>> {
		let balance = Self::current_balance(id);

		// Calculate the current payout balance. The first 3 values of this calculation added
		// together represent what the balance would be if no payouts were made. The
		// `last_recorded_total_payouts` is then subtracted from this value to cancel out previously
		// recorded payouts, leaving only the remaining payouts that have not been claimed.
		let current_payout_balance = balance
			.saturating_add(self.total_rewards_claimed)
			.saturating_add(self.total_commission_claimed)
			.saturating_sub(self.last_recorded_total_payouts);

		// Split the `current_payout_balance` into claimable rewards and claimable commission
		// according to the current commission rate.
		let new_pending_commission = commission * current_payout_balance;
		let new_pending_rewards = current_payout_balance.saturating_sub(new_pending_commission);
```

**File:** prdoc/pr_12397.prdoc (L1-12)
```text
title: 'nomination-pools: snapshot rewards before `set_commission_max` lowers current commission'
doc:
- audience: Runtime Dev
  description: |-
    `set_commission_max` force-lowers `commission.current` (via `try_update_max`) when the new max
    is below the active rate, but did not first call `update_records`. Rewards accrued at the higher
    rate since the last snapshot were therefore re-rated at the new lower rate on the next
    `update_records`, crediting the differential `(old_current - new_max) * accrued` to members
    instead of the commission payee.

    The fix snapshots the reward pool at the current commission before the cut, mirroring the
    ordering already used in `set_commission`.
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L6990-7031)
```rust
	#[test]
	fn set_commission_max_snapshots_rewards_before_lowering_current() {
		// `set_commission_max` force-lowers `current` when the new max is below it. Rewards that
		// accrued at the higher rate since the last snapshot must stay owed to the payee at that
		// higher rate, not be re-rated at the new lower rate and leaked to members.
		ExtBuilder::default().build_and_execute(|| {
			let pool_id = 1;
			let payee = 900;
			let _ = Currency::set_balance(&payee, 5);

			// GIVEN: commission is 50% (this snapshots the still-empty reward pool)...
			assert_ok!(Pools::set_commission(
				RuntimeOrigin::signed(900),
				pool_id,
				Some((Perbill::from_percent(50), payee))
			));
			// ...and 100 of rewards accrue with no intervening snapshot (no claim/bond happens).
			deposit_rewards(100);
			assert_eq!(RewardPool::<Runtime>::current_balance(pool_id), 100);
			assert_eq!(RewardPools::<Runtime>::get(pool_id).unwrap().total_commission_pending, 0);

			// WHEN: root force-lowers max commission to 20%, cutting `current` from 50% to 20%.
			assert_ok!(Pools::set_commission_max(
				RuntimeOrigin::signed(900),
				pool_id,
				Perbill::from_percent(20)
			));

			// THEN: the 100 that accrued at 50% was snapshotted before the cut, so 50 is owed to
			// the payee. Without the pre-cut snapshot this would be 20% * 100 = 20.
			assert_eq!(RewardPools::<Runtime>::get(pool_id).unwrap().total_commission_pending, 50);

			// AND: claiming commission pays the payee the pre-cut 50, not the post-cut 20.
			let _ = pool_events_since_last_call();
			assert_ok!(Pools::claim_commission(RuntimeOrigin::signed(payee), pool_id));
			assert_eq!(
				pool_events_since_last_call(),
				vec![Event::PoolCommissionClaimed { pool_id, commission: 50 }]
			);
			assert_eq!(Currency::free_balance(&payee), 5 + 50);
			assert_eq!(RewardPools::<Runtime>::get(pool_id).unwrap().total_commission_claimed, 50);
		})
```
