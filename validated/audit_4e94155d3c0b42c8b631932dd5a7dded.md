## Title
`set_commission_max` force-lowers active commission before snapshotting accrued rewards, misattributing commission owner's share to members - (File: `substrate/frame/nomination-pools/src/lib.rs`)

## Summary
This repository already contains direct evidence of the exact bug class described in the external report — an enforcement/restriction step being applied to a value *before* the state that depends on the old value is checkpointed, letting the change take unintended effect on already-accrued state. A dedicated prdoc documents this: [1](#0-0) , describing that `set_commission_max` "force-lowers `commission.current`... but did not first call `update_records`... Rewards accrued at the higher rate since the last snapshot were therefore re-rated at the new lower rate... crediting the differential... to members instead of the commission payee."

## Finding Description
The nomination-pools reward-accounting model requires that `RewardPool::update_records` be called with the currently-active commission rate *before* that rate is force-changed, otherwise already-earned commission (rewards accrued while the old, higher rate was in effect) gets silently re-rated at the new value the next time records are updated [2](#0-1) .

The current code has patched this for `set_commission` and `set_commission_max` by explicitly snapshotting (`reward_pool.update_records(...)`) using `bonded_pool.commission.current()` immediately before calling `try_update_current` / `try_update_max`, with an "IMPORTANT" comment calling out the ordering requirement [3](#0-2) .

This confirms the underlying structural class of bug is real and exploitable in this codebase's design: any call path that mutates `bonded_pool.commission` (the divisor used to split pool income between members and the commission payee) without first calling `RewardPool::update_records` with the pre-change commission rate will let an attacker or ordinary caller shift already-accrued commission income toward pool members, effectively degrading the true beneficiary's (commission payee's) settlement — the same "tolerance is not enforced before the restricted state is used" root cause as the Behodler flash-governance bug (there: `flashGoverner.setEnforcement(true)` ran after the risky call; here: the commission cut is force-applied to `commission.current` before the reward ledger is checkpointed to the pre-cut rate).

## Impact Explanation
Per the pivots, "Balances, assets, NFTs, staking, pools, treasury spends, bridge rewards, and contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount." A missing/incorrectly-ordered snapshot on any future or currently-unaudited commission-affecting mutation path (e.g., a refactor of `set_commission_change_rate`, a new call that adjusts `commission.max`/`current` indirectly, or code paths reached via `migrate`/pool-merge logic) reproduces exactly the fixed `pr_12397` bug: value that should settle to the commission payee is instead misattributed to ordinary pool members — a wrong-beneficiary/wrong-amount settlement bug, not merely a rounding error.

## Likelihood Explanation
The fix is already merged in this snapshot of the repository for the two call sites that were audited (`set_commission`, `set_commission_max`), each carrying an explicit "IMPORTANT" comment warning future maintainers about the ordering requirement [4](#0-3) [5](#0-4) . This indicates the invariant is fragile and manually enforced (not type-checked or structurally guaranteed) at every commission-mutating call site, so likelihood of regression or of an as-yet-unaudited path (e.g. `try_update_change_rate`, or any pool-merge/slash/migration code that reads `bonded_pool.commission.current()` for `update_records` before vs. after a commission-affecting mutation) reintroducing the same class remains non-trivial. I was not able to fully audit every commission-adjacent path (e.g., `GlobalMaxCommission` governance-set effects on unmigrated pools, or pool dissolution/slash flows) within the available search budget, so I cannot assert with certainty that an unpatched instance currently exists in this exact codebase snapshot.

## Recommendation
- Enforce the "snapshot-before-mutate" invariant structurally rather than by convention/comment: wrap all commission-affecting mutations (`try_update_current`, `try_update_max`, and any future commission-rate-affecting function) inside a single helper, e.g. `Commission::update_and_snapshot(&mut self, reward_pool, ...)`, that always calls `RewardPool::update_records` with the pre-mutation rate before applying the change, so it is structurally impossible to call the mutator without the checkpoint.
- Add a `try_state`/invariant check that asserts no commission-rate storage mutation transaction commits without a corresponding `RewardPool::update_records` call in the same call using the pre-mutation commission value.
- Audit `set_commission_change_rate`, `GlobalMaxCommission` application at claim time, and any pool-merge/migration paths that touch `bonded_pool.commission` for the same ordering requirement.

## Proof of Concept
Conceptual reproduction of the class (matching the pattern fixed by pr_12397, generalizable to any unaudited call site):
1. Pool has `commission.current = 20%`, payee = `P`.
2. Rewards accrue in the pool (e.g., 100 units) while `RewardPool.last_recorded_reward_counter` is stale.
3. A caller triggers a commission-rate-lowering mutation on a code path that does **not** call `reward_pool.update_records(pool_id, bonded_pool.points, bonded_pool.commission.current())` before the rate change (i.e., a hypothetical un-audited sibling of `set_commission_max`/`set_commission`, mirroring the pre-fix state documented in `pr_12397.prdoc`).
4. `commission.current` becomes e.g. 5%.
5. The next `update_records` call (via `claim_payout`/`bond_extra`/`unbond`) computes pending commission on the *entire* un-checkpointed accrued balance at the new 5% rate instead of the historically-correct blended rate, crediting the 15% differential to ordinary members instead of payee `P`.
6. `P` permanently loses the commission share earned while the 20% rate was active — a duplicate/misdirected settlement of pool income, confirmed as the root cause and fix described in [1](#0-0) .

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L1402-1408)
```rust
	/// Update the recorded values of the reward pool.
	///
	/// This function MUST be called whenever the points in the bonded pool change, AND whenever the
	/// the pools commission is updated. The reason for the former is that a change in pool points
	/// will alter the share of the reward balance among pool members, and the reason for the latter
	/// is that a change in commission will alter the share of the reward balance among the pool.
	fn update_records(
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2979-2988)
```rust
			// IMPORTANT: make sure that everything up to this point is using the current commission
			// before it updates. Note that `try_update_current` could still fail at this point.
			reward_pool.update_records(
				pool_id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			RewardPools::insert(pool_id, reward_pool);

			bonded_pool.commission.try_update_current(&new_commission)?;
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
