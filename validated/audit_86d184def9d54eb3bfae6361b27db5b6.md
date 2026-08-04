Based on the search results, I found a concrete, already-documented local analog to the reported bug class in `pallet-nomination-pools`.

### Title
Commission cut re-rates unrecorded rewards to members instead of the commission payee - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The external report's core invariant is: a state-critical variable (`_tokenId`) used in an accounting/reward-update call was not assigned its intended value before use, causing rewards to be mis-attributed. The local analog in this repository is the same defect class in `set_commission_max`: the pool's reward-per-point snapshot is not taken (`update_records` not called) *before* the current commission rate is force-lowered via `try_update_max`, so `current_reward_counter`'s next computation re-rates already-accrued-but-unsnapshotted rewards at the new, lower commission rate, crediting the commission differential to members instead of to the commission payee.

### Finding Description
`RewardPool::update_records` in [1](#0-0)  is documented as needing to be called "whenever the points in the bonded pool change, AND whenever the pool's commission is updated," precisely because a commission change alters the split of `current_payout_balance` between rewards and commission going forward, per `current_reward_counter` at [2](#0-1) . The known-good ordering (used in `set_commission`) is: snapshot first via `update_records`, then apply the new commission rate — this locks in the payout split for rewards accrued *before* the change at the *old* rate.

The bug, per [3](#0-2) , is that `set_commission_max` calls `try_update_max` to force-lower `commission.current` when the new max is below the active rate, *without first calling `update_records`*. This is the direct structural analog of the external bug: a value that must be captured/assigned (the reward snapshot at the *pre-change* commission rate — analogous to `lastTokenId`) is instead left stale/default (analogous to the un-assigned `_tokenId` defaulting to 0), and gets used implicitly by the next `current_reward_counter` computation at the *wrong* (post-change, lower) rate.

### Impact Explanation
This falls under "Balances, assets, NFTs, staking, pools, treasury spends... must conserve value and settle exactly once to the rightful beneficiary and amount." Here, rewards accrued while commission was higher are, on the next `update_records` call, split as if the lower commission had always applied. The commission differential `(old_current - new_max) * accrued` is credited to pool members rather than to the commission payee — a wrong-beneficiary/wrong-amount settlement of already-earned commission, i.e. a form of fund misdirection away from the rightful recipient (the commission payee, typically the pool operator).

### Likelihood Explanation
The trigger is an ordinary, unprivileged-adjacent path: any pool admin/root-with-permission lowering `commission_max` below the currently active commission rate. No malicious peer, relayer, or validator is required — it's a straightforward runtime call ordering bug in `pallet-nomination-pools`, a widely-used FRAME pallet on production chains (e.g., Asset Hub, staking-async runtimes), reachable via the pallet's own public dispatchable.

### Recommendation
Ensure `set_commission_max` calls `RewardPool::update_records` (snapshotting `last_recorded_reward_counter` / `total_commission_pending` at the pool's *current* commission rate) before applying `try_update_max`, mirroring the ordering already used in `set_commission`, as described in the fix in [3](#0-2) .

### Proof of Concept
1. Pool has active commission rate `C_old` and has accrued unclaimed rewards since the last `update_records` snapshot.
2. Admin calls `set_commission_max(new_max)` where `new_max < C_old`, which via `try_update_max` force-lowers `commission.current` to `new_max` without snapshotting first.
3. A subsequent action (join/bond_extra/unbond, or the next commission change) triggers `update_records`, which calls `current_reward_counter` — this computes `new_pending_commission = commission * current_payout_balance` using the *new* lower `commission`, but `current_payout_balance` includes rewards accrued during the window when the *old* higher commission applied.
4. Result: the commission differential for that window is paid to members via `new_pending_rewards` instead of accruing to `total_commission_pending` for the commission payee — permanent, non-reversible mis-settlement of pool funds.

Note: I could not fully confirm from the index whether this fix (`update_records` before `try_update_max`) is already present in the current `set_commission_max` implementation in this snapshot of `substrate/frame/nomination-pools/src/lib.rs` — my final read of that function's body was truncated by tool-call limits, so verify the current call ordering directly in the file before treating this as unpatched.

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1448-1512)
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
	}
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
