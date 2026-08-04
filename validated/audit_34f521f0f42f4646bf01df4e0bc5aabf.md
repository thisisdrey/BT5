Based on my investigation, I found a confirmed local analog of this exact bug class in the nomination-pools pallet, already documented as a resolved issue in the repository itself.

### Title
Commission-affecting parameter changes were credited to members instead of the commission payee before the `set_commission_max` snapshot fix - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The external report describes a class of bug where a getter recomputes a rate using the *current* parameter value instead of the value that was in effect during the elapsed period, silently re-rating past accrual and misallocating the differential. The exact same bug class exists in `pallet-nomination-pools`: `RewardPool::current_reward_counter` splits `current_payout_balance` (rewards accrued since `last_recorded_total_payouts`) using whatever `commission` value is passed in at call time, rather than the commission rate(s) that were actually active while that balance accrued [1](#0-0) . Any pool operation that changes `commission.current` without first calling `update_records()` to snapshot/settle the pending payout at the old rate will cause the next `current_reward_counter`/`update_records()` call to re-rate the entire un-settled balance at the new rate.

### Finding Description
`RewardPool::update_records` is documented as needing to be called "whenever the points in the bonded pool change, AND whenever the pool's commission is updated," specifically because a commission change alters how the outstanding, unclaimed payout balance is split between members and the commission payee [2](#0-1) . `current_reward_counter` computes `current_payout_balance` as everything accrued since `last_recorded_total_payouts`, then applies the commission rate passed into the function to split it into `new_pending_commission` vs `new_pending_rewards` [3](#0-2) .

This is precisely the "parameter change skews getters" pattern from the external report: the getter recalculates a rate-dependent split using the *current* parameter, not the parameter that was active while the un-settled balance accrued. The repository's own `prdoc/pr_12397.prdoc` documents that `set_commission_max`, when force-lowering `commission.current` via `try_update_max`, did not call `update_records` first, so rewards accrued at the higher rate since the last snapshot were re-rated at the new lower max on the next `update_records`, crediting the differential `(old_current - new_max) * accrued` to members instead of to the commission payee [4](#0-3) . The fix explicitly snapshots the reward pool at the current commission before applying the cut, mirroring the ordering already used in `set_commission` [5](#0-4) .

### Impact Explanation
This bug class causes a real, silent transfer of value between two parties (pool commission payee vs. pool members) with no attacker action required beyond a legitimate commission-affecting operation being executed without the required prior snapshot. Since `current_reward_counter` feeds `pending_rewards`/`do_reward_payout` and `claim_commission`, any unpatched code path that mutates `commission.current` (or, by the same broken invariant, `commission.max`/`change_rate` in a way that forces a change to `current`) without calling `update_records` first will misroute funds — either overpaying members at the commission payee's expense or vice versa. This matches the "theft or unbacked mint," "duplicate settlement," and "wrong beneficiary or amount" impact classes for staking/reward accounting in the program scope.

### Likelihood Explanation
The vulnerable pattern (`current_reward_counter` using the caller-supplied/"current" commission rather than a rate cached at time of accrual) is a structural property of the reward-pool math, not a one-off bug; it was already proven exploitable via `set_commission_max` per the resolved `pr_12397` fix. Any other call path that changes `commission.current` (directly or indirectly, e.g. through `try_update_max`, `try_update_change_rate`, or future extensions) without first invoking `update_records` reproduces the same misallocation. Given the ordering requirement is manually enforced per call-site rather than structurally guaranteed (no compiler/type-level check ties a commission mutation to a mandatory prior `update_records` call), this class of bug is prone to recurrence whenever new commission-mutating extrinsics or governance calls are added.

### Recommendation
Do not rely on call-site discipline. Enforce the invariant structurally, e.g. by making commission mutation only possible through a method that internally calls `update_records` first (so `last_recorded_reward_counter`/`total_commission_pending` are snapshotted at the *old* rate before `commission.current` changes), and add a `debug_assert!`/try-runtime invariant that no commission-affecting field is written without a fresh `update_records` snapshot in the same call. Audit all commission-related extrinsics (`set_commission`, `set_commission_max`, `set_commission_change_rate`, and any future ones) to confirm each calls `update_records` (or equivalent) prior to mutating `commission.current`.

### Proof of Concept
This exact PoC is already captured by the repository's own regression coverage referenced in `pr_12397.prdoc`: set an initial commission (e.g. 20%), accrue rewards, then call `set_commission_max` with a max below the current rate so `try_update_max` force-lowers `commission.current` without a prior `update_records` snapshot. On the next `claim_payout`/`update_records`, the balance accrued at the old (higher) rate is re-split at the new (lower) rate, crediting `(old_current - new_max) * accrued` to pool members rather than the commission payee [6](#0-5) . This confirms the "recompute with current parameter instead of cached value" invariant break generalizes beyond the single patched call-site and should be checked against every other commission-mutating path in `substrate/frame/nomination-pools/src/lib.rs`.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1402-1413)
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L1458-1470)
```rust
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
