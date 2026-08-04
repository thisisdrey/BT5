Based on my research, the exact bug-class analog to the BendDAO fee-factor issue already exists (and has been *fixed*) in this repository's `pallet-nomination-pools`, but a second, unfixed instance of the same root cause remains live.

### Title
Global commission cap changes retroactively re-rate already-accrued pool rewards to the commission payee's detriment - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
`Commission::current()` reads `self.current` and clamps it with `.min(GlobalMaxCommission::<T>::get())` at call time [1](#0-0) . `RewardPool::update_records` uses whatever `commission` value is passed to it (i.e., the live, clamped `current()`) to split newly-observed pool income between `total_commission_pending` and members' reward counter, based on the balance delta since `last_recorded_total_payouts`/`last_recorded_reward_counter` [2](#0-1) [3](#0-2) . This is exactly the BendDAO pattern: a rate parameter (`feeFactor` there, effective commission `current()` here) is changed, but the value it will be applied to (accrued-but-unrecorded interest/rewards) was not snapshotted at the old rate first.

### Finding Description
The repository already fixed the pool-local version of this bug: `set_commission_max` now calls `reward_pool.update_records(...)` with the pre-change `bonded_pool.commission.current()` *before* calling `try_update_max`, explicitly to avoid re-rating already-accrued rewards at the new, lower rate [4](#0-3) , documented in [5](#0-4) .

However, `GlobalMaxCommission` is a single storage item that clamps the effective commission of *every* pool simultaneously via the `.min(...)` in `current()` [1](#0-0) . When this global value is lowered, there is no corresponding per-pool snapshot step — no code iterates over all pools' `RewardPools` and calls `update_records` with the *old* effective commission before the global cap takes effect. The very next `update_records` call for any affected pool (triggered permissionlessly by `bond_extra`, `unbond`, `claim_payout`, or `set_commission`) will use the *new*, lower `current()` to split the entire backlog of unrecorded income accrued since the last snapshot, silently crediting the differential to the members' reward counter instead of the commission payee — the same accounting inconsistency the BendDAO report described, just triggered by a chain-wide parameter instead of a per-contract one.

### Impact Explanation
Any pool with pending, unrecorded rewards at the moment `GlobalMaxCommission` decreases will have part of its rightfully-earned commission permanently reallocated to members instead of the commission payee, with no way to recover it (once `update_records` runs, `total_commission_pending`/`last_recorded_reward_counter` are updated and the pre-change split is unrecoverable). This is a real, silent, un-backed value transfer between distinct beneficiaries (commission payee vs. members) inside a staking-reward accounting pallet, matching the "duplicate settlement or wrong beneficiary/amount" impact category.

### Likelihood Explanation
`GlobalMaxCommission` changes are rare (typically root/governance-driven) but the trigger for materializing the loss is *any permissionless call* that invokes `update_records` afterward (`bond_extra`, `unbond`, `claim_payout`, `set_commission`), so exploitation of the window requires no privileged action — only that some reward income accrued between the last snapshot and the global cap reduction, which is common in active pools. The severity is comparable to what BendDAO judges assessed for the analogous issue: low/medium likelihood but confirmed and worth fixing, consistent with the fact that this repo's maintainers already fixed the pool-level analog (`pr_12397`) for the identical reasoning.

### Recommendation
When `GlobalMaxCommission` is updated (or at minimum, in the setter for it), iterate/snapshot all `RewardPools` whose `commission.current()` exceeds the new cap by calling `update_records` with their pre-change effective commission before the new global cap is stored — mirroring the fix already applied to `set_commission_max`. Alternatively, redesign `current()` so that clamping to `GlobalMaxCommission` is captured by explicitly registering the update (as `try_update_max` does for pool-level max) rather than applying it live and retroactively at read time inside `update_records`.

### Proof of Concept
1. Pool `P` sets commission to 50% via `set_commission` (this snapshots the reward pool) [6](#0-5) .
2. 100 units of staking rewards accrue into the pool with no intervening `update_records` call (no bond/unbond/claim).
3. Governance lowers `GlobalMaxCommission` to 20% (a call outside pool-level `set_commission_max`, so no pool-level snapshot occurs).
4. Any member calls `claim_payout`, which calls `update_records` using `bonded_pool.commission.current()` — now clamped to 20% via the `.min(GlobalMaxCommission)` in `current()` [1](#0-0) .
5. `total_commission_pending` is credited only 20 (20% of 100) instead of the 50 that should have been owed to the payee at the rate in effect while the rewards accrued — the missing 30 is instead paid to members, exactly mirroring the already-acknowledged `set_commission_max` bug fixed in [7](#0-6)  but left unaddressed for the global-cap path.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L843-850)
```rust
	/// Gets the pool's current commission, or returns Perbill::zero if none is set.
	/// Bounded to global max if current is greater than `GlobalMaxCommission`.
	fn current(&self) -> Perbill {
		self.current
			.as_ref()
			.map_or(Perbill::zero(), |(c, _)| *c)
			.min(GlobalMaxCommission::<T>::get().unwrap_or(Bounded::max_value()))
	}
```

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1448-1471)
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2977-2986)
```rust
			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: make sure that everything up to this point is using the current commission
			// before it updates. Note that `try_update_current` could still fail at this point.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3016-3030)
```rust
			let mut reward_pool = RewardPools::<T>::get(pool_id)
				.defensive_ok_or::<Error<T>>(DefensiveError::RewardPoolNotFound.into())?;
			// IMPORTANT: snapshot rewards accrued at the current commission before `try_update_max`
			// can force-lower it. Otherwise rewards accrued since the last snapshot would be
			// re-rated at the new (lower) rate and the differential credited to members instead
			// of the commission payee. Mirrors the ordering in `set_commission`.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);

			bonded_pool.commission.try_update_max(pool_id, max_commission)?;
			bonded_pool.put();
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L7018-7020)
```rust

```

**File:** prdoc/pr_12397.prdoc (L1-15)
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
crates:
- name: pallet-nomination-pools
  bump: patch
```
