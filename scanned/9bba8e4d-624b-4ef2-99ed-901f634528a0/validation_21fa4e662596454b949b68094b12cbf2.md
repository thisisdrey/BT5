### Title
Nomination Pool commission accounting lumps multi-period reward accrual under a single (possibly stale) commission rate - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The external report describes a fee-accounting bug where a rate value (Lido Hurdle rate) is only re-evaluated when an update transaction lands, so delayed updates cause several days of accrual to be compressed into a single "snapshot" application of the rate, artificially inflating the fee taken. `pallet-nomination-pools` has the same structural pattern in `RewardPool::update_records` / `current_reward_counter`: unclaimed pool reward balance accumulated since the *last* call to `update_records` is split between members and the pool commission using whatever commission percentage is *currently in storage at call time*, with no accounting for how much of that accrued balance was earned before vs. after any intervening commission change.

### Finding Description
`RewardPool::current_reward_counter` computes `current_payout_balance` as the entire pool balance change since `last_recorded_total_payouts` [1](#0-0) , and then splits that whole backlog into commission vs. member rewards using the `commission` parameter passed in at call time [2](#0-1) .

`update_records`, which stores the resulting `last_recorded_reward_counter` / `last_recorded_total_payouts` snapshot, is documented as something that "MUST be called whenever the points in the bonded pool change, AND whenever the pool's commission is updated" [3](#0-2) . This is a caller-maintained invariant, not one enforced structurally for every block/era — the reward counter is only re-based when some pool interaction (bond, unbond, claim, or commission change) actually happens. If no such interaction occurs for several eras (analogous to the offchain bot missing several days of updates in the report), then whenever the next interaction finally triggers `update_records`, the *entire multi-era backlog* of accrued reward is split using only the single `commission` value that is active at that moment — exactly the "compounding effect of multiple updates being processed at the same time, with no time aspect considered" failure mode described in the report.

Concretely: if a pool's commission is raised from e.g. 5% to 20% while no bond/unbond/claim happens for several eras, the entirety of the reward that accrued while the low commission was nominally "in effect" ends up commissioned at the new, higher rate as soon as the next state-changing call finally executes `update_records`. Conversely, if commission is lowered, members are shorted relative to what should have accrued under the higher rate that was in effect for most of the elapsed period. Either way, `current_payout_balance` is not time-weighted or bucketed by which commission rate applied to which sub-interval — the whole elapsed balance is stamped with one rate.

### Impact Explanation
This misallocates real value between the pool's commission-taker and its members: the same aggregate rewards get split in proportions that do not correspond to the actual commission rate that was in force during each sub-interval of accrual. Because `last_recorded_total_payouts` is only ever updated on-demand, the size of the mis-split backlog grows with the length of the gap between triggering interactions, so a bigger delay produces a bigger single-shot mis-allocation, mirroring the "increase in fee" pattern in the source report. Total token supply/value is still conserved (nothing is minted or burned incorrectly), but the amount attributed to commission vs. staker rewards is wrong, which is a "wrong beneficiary or amount" settlement problem for real staked funds.

### Likelihood Explanation
Likelihood is comparable to the original report's characterization: normally pools see frequent member activity (bonding/unbonding/reward claims) which calls `update_records` often enough that the window between snapshots is small, so the practical drift is bounded. It becomes material only when a pool goes through an extended low-activity period spanning a commission change and multiple reward-bearing eras, which is a plausible but not routine occurrence — the same "low likelihood, but real when it happens" profile the report itself assigns to the Lido case.

### Recommendation
Either (a) force a mandatory reward-counter checkpoint on every era transition (not just on user-triggered pool state changes), so no backlog can span a commission change, or (b) redesign `current_reward_counter` to track commission changes with their own effective-timestamp/era markers and split `current_payout_balance` proportionally by sub-interval rather than applying a single rate to the whole backlog.

### Proof of Concept
1. Pool P has commission 5%; some rewards accrue and are left unclaimed for several eras with no member bonding/unbonding/claiming activity (so `update_records` is not invoked).
2. Pool root calls `set_commission` to raise the rate to 20%. Internally this triggers `update_records`, which computes `current_reward_counter` using `commission = 20%` [4](#0-3)  over the *entire* backlog of reward balance accumulated across all the eras since the last snapshot, even though 5% was nominally in effect for most of that period.
3. `total_commission_pending` is credited with 20% of the whole backlog rather than a time-weighted blend of 5% (pre-change period) and 20% (post-change period), permanently shorting pool members of the difference — funds they should have received are instead routed to the commission pot.

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1450-1470)
```rust
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
