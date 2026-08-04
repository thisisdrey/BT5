### Title
`set_commission_max` can force-lower a pool's active commission without snapshotting prior rewards, misdirecting accrued commission to members - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The external report's core broken invariant is: a state-dependent, time-varying parameter (liquidation threshold based on leverage) is recalculated using the *current* configuration only at specific trigger points, but a related update path skips the required "snapshot-then-apply-new-config" step, letting stale, more favorable accounting persist across a parameter change. The same class of bug exists in `pallet-nomination-pools`: commission is a rate that accrues against a reward pool over time, and any change to the effective/max commission rate must first "snapshot" (via `update_records`) the rewards accrued at the *old* rate before the new rate takes effect, otherwise past accrual gets mis-rated retroactively.

### Finding Description
In `pallet-nomination-pools`, commission accrual is tracked lazily: `RewardPool::update_records` is the function that settles pending rewards/commission at the pool's currently configured commission rate before any commission parameter changes, mirroring how the trading protocol should recompute liquidation price using the config that was active at the time, then apply the new config going forward. `set_commission` already calls `update_records` before mutating the commission struct, which is the correct pattern.

However, `set_commission_max` mutates `commission.max` and, via `try_update_max`, force-lowers `commission.current` down to the new max when the new max is below the currently active rate — without first calling `update_records` to snapshot the reward pool at the *old* current rate. This is structurally identical to the reported bug: `getLiqPnlThresholdP` is only invoked with fresh trade state at explicit callback points, and if a caller mutates configuration-dependent state (leverage/position size) without re-deriving from the currently valid parameters, stale accounting persists. Here, rewards that accrued while the higher commission rate was in effect get retroactively re-rated at the new, lower commission rate the next time `update_records` runs (e.g., on the next `bond_extra`, `claim_payout`, or `claim_commission`), because `update_records` computes commission owed using whatever `commission.current` is set to *at the time it runs*, not the rate that was active while the reward accrued.

This was already identified and remediated upstream (see `prdoc/pr_12397.prdoc`, titled "nomination-pools: snapshot rewards before `set_commission_max` lowers current commission"), confirming the bug-class is real and applicable to this codebase: [1](#0-0) . The fix's own description states the exact defect: `set_commission_max` "force-lowers `commission.current`... via `try_update_max`... but did not first call `update_records`," causing the differential `(old_current - new_max) * accrued` to be credited to members instead of the commission payee.

The correct ordering pattern is demonstrated by `do_bond_extra` and `do_claim_commission`, both of which call `reward_pool.update_records(pool_id, bonded_pool.points, bonded_pool.commission.current())` before any mutation that depends on the commission rate: [2](#0-1) [3](#0-2) 

If `set_commission_max` (and any other extrinsic that can force-change `commission.current`, e.g. commission throttling changes) mutates the rate without this snapshot step, the next `update_records` call re-rates the entire un-snapshotted accrual window at the new rate, silently transferring value from the commission payee (root/nominator-designated commission receiver) to the pool members' claimable balance.

### Impact Explanation
This breaks the "conserve value and settle exactly once to the rightful beneficiary and amount" invariant for treasury/reward payouts: commission that was rightfully owed to the pool's commission payee under the previously active rate is instead paid out to ordinary pool members. Impact is Medium: it does not create tokens out of thin air, but it misdirects legitimately accrued commission funds to the wrong beneficiary, which is a real fund-diversion bug matching the "duplicate settlement or payout" / wrong-beneficiary pivot in scope.

### Likelihood Explanation
Likelihood is Medium: it requires (a) a pool with an active commission rate and pending unclaimed rewards, and (b) a call to `set_commission_max` (which can be triggered by the pool's root/commission-admin role, an action any pool root can legitimately take, not requiring any privileged governance actor) that lowers the max below the current rate. No malicious relayer, validator, or admin abuse is needed — this is triggerable by ordinary pool governance in the normal course of adjusting commission caps.

### Recommendation
Ensure every code path that can mutate `commission.current` (directly or indirectly, including `try_update_max`/`set_commission_max`, and any future throttling/rate-limit config changes) calls `RewardPool::update_records` with the pool's current (pre-change) commission rate before applying the new rate, mirroring the ordering already used in `set_commission`. Add regression tests asserting that rewards accrued before a `set_commission_max` call are settled at the old rate, and only rewards accrued after the call are settled at the new rate.

### Proof of Concept
1. Create a pool with commission rate `R1` (e.g. 50%) and `commission.max` unset or high.
2. Members bond; rewards `X` accrue into the reward pool over several blocks — accrued commission owed to the payee is `R1 * X`, but `update_records` has not yet been called since accrual, so it is still "unsettled" in the reward-counter delta.
3. Pool root calls `set_commission_max` with a new max `R2 < R1`, which via `try_update_max` force-lowers `commission.current` to `R2` — without calling `update_records` first.
4. Any member now calls `bond_extra` or `claim_payout` (which internally trigger `update_records(pool_id, points, commission.current())`), settling the entire pre-existing unsettled reward window `X` at rate `R2` instead of `R1`.
5. Commission payee receives `R2 * X` instead of the rightfully accrued `R1 * X`; the shortfall `(R1 - R2) * X` is instead distributed to members as if it were ordinary reward, permanently diverting funds from the commission payee.

### Citations

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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3671-3677)
```rust
		// payout related stuff: we must claim the payouts, and updated recorded payout data
		// before updating the bonded pool points, similar to that of `join` transaction.
		reward_pool.update_records(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3716-3722)
```rust
		// IMPORTANT: ensure newly pending commission not yet processed is added to
		// `total_commission_pending`.
		reward_pool.update_records(
			pool_id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
```
