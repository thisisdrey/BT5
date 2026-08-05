Based on my research, I found a concrete division-by-zero analog in the nomination-pools reward-accounting logic that structurally mirrors the ALCX `bumpExchangeRate` bug (a ratio calculation that divides by a "supply-like" quantity which can legitimately be zero while a non-zero balance still sits in the associated account).

### Title
`RewardPool::current_reward_counter` divides by `bonded_pool.points`, which can be zero while the reward account still holds a non-zero balance, bricking reward-accounting entrypoints - (File: substrate/frame/nomination-pools/src/lib.rs)

### Summary
`RewardPool::current_reward_counter` computes each pool member's share of unclaimed rewards via `T::RewardCounter::checked_from_rational(new_pending_rewards, bonded_points)` [1](#0-0) . This is the pools-pallet analog of gALCX's `exchangeRate += (balance * exchangeRatePrecision) / totalSupply` — a ratio calculation over a "supply" denominator (`bonded_pool.points`, the pool-wide equivalent of `totalSupply`) that assumes the denominator is non-zero whenever the corresponding balance (`current_payout_balance`, held in the reward account) is non-zero.

### Finding Description
`bonded_points` plays the role of `totalSupply` in the external report, and the reward account's `current_balance` plays the role of `balance` (ALCX held directly by the contract). The pallet's own `try_runtime` invariant check explicitly special-cases `bonded_pool.points.is_zero()` before calling `current_reward_counter`, skipping the reward-counter computation and noting "this pool has been heavily slashed and cannot have any rewards anymore" [2](#0-1) . This demonstrates the pallet authors are aware that `bonded_pool.points == 0` is a reachable state for an existing (non-destroyed) pool. `RewardPool::update_records`, which is invoked whenever bonded-pool points change or commission is updated, calls `current_reward_counter` without this guard [3](#0-2) . If `bonded_points` is zero at the moment `update_records` runs, `checked_from_rational` returns `None`, which propagates as `Error::<T>::OverflowRisk` and aborts the caller's extrinsic (e.g. `set_commission`, `set_commission_max`, or other flows that must call `update_records` before mutating pool state) — exactly analogous to `bumpExchangeRate` reverting and freezing `stake`/`unstake`/`migrateSource` in the ALCX report.

Separately, `do_reward_payout` (the live path behind `claim_payout`) calls the same `current_reward_counter` guarded only by `!member.active_points().is_zero()` [4](#0-3) , not by `bonded_pool.points`. If any accounting drift or edge case leaves a member with non-zero `active_points()` while `bonded_pool.points` reads zero, `claim_payout` would hard-fail with `OverflowRisk` for that member permanently, since the failure is deterministic given the stored state and cannot be self-healed by an unprivileged caller.

### Impact Explanation
Where reachable, this denominator-zero condition causes a stuck/bricked pool state: reward-related dispatchables (commission updates, reward-counter refresh paths, and potentially `claim_payout` for affected members) deterministically fail with `OverflowRisk`, freezing legitimate reward distribution and pool administration — the same "unusable contract" class of impact described in the ALCX report (medium severity: DoS, not fund loss, recoverable only via pool-level remediation).

### Likelihood Explanation
Confidence is limited: I could not fully trace, within the available search budget, the exact live call sites of `update_records` and confirm a concrete unprivileged sequence that drives `bonded_pool.points` to exactly zero while `RewardPool::current_balance` remains positive and the pool is still active (not yet destroyed/removed). The pallet's defensive `try_runtime` check strongly suggests this state is anticipated as reachable in general pool lifecycles (e.g. via full unbonding to zero points before pool teardown, or heavy slashing scenarios), but I was not able to enumerate every mutation path to `bonded_pool.points` and rule out that all of them are gated before reaching `update_records`/`current_reward_counter`. This should be treated as a suspected-but-not-fully-confirmed reachability gap rather than a fully proven exploit chain.

### Recommendation
Add an explicit `bonded_points.is_zero()` guard in `RewardPool::current_reward_counter` (mirroring the guard already present in `try_state`), short-circuiting to "no new rewards accrue" instead of calling `checked_from_rational` with a zero denominator, so that `update_records` and `do_reward_payout` cannot fail with `OverflowRisk` purely due to a legitimately-zero points total.

### Proof of Concept
Not fully constructable with the available evidence — a concrete extrinsic sequence that leaves `bonded_pool.points == 0` for a still-active pool with a non-zero reward-account balance, followed by a call that triggers `update_records`/`current_reward_counter`, would need to be validated against the full unbonding/slashing/pool-destruction state machine in `substrate/frame/nomination-pools/src/lib.rs`, which I could not completely trace in this session.

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3536-3543)
```rust
		// a member who has no skin in the game anymore cannot claim any rewards.
		ensure!(!member.active_points().is_zero(), Error::<T>::FullyUnbonding);

		let (current_reward_counter, _) = reward_pool.current_reward_counter(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3985-3992)
```rust
			if !bonded_pool.points.is_zero() {
				let commission = bonded_pool.commission.current();
				let (current_rc, _) = reward_pool
					.current_reward_counter(d.pool_id, bonded_pool.points, commission)
					.unwrap();
				let pending_rewards = d.pending_rewards(current_rc).unwrap();
				*pools_members_pending_rewards.entry(d.pool_id).or_default() += pending_rewards;
			} // else this pool has been heavily slashed and cannot have any rewards anymore.
```
