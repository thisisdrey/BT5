Audit Report

## Title
Nomination-pools reward payout is sandwichable via `join` + `payout_stakers`, letting an attacker steal a share of era rewards earned entirely by other members - (File: `substrate/frame/nomination-pools/src/lib.rs`)

## Summary
`pallet-nomination-pools` distributes newly landed reward-account funds to members based on their **current** points share rather than the points they held while the reward was accruing in `pallet-staking`. Because `payout_stakers`/`payout_stakers_by_page` is permissionless and delivers an entire historical era's reward as a single lump transfer, and `join` is permissionless with no cooldown, an attacker can atomically join a pool with a large bond, trigger (or wait for) the payout, and claim a share of a reward earned entirely before they joined, diluting legitimate members.

## Finding Description
`RewardPool::current_reward_counter` (`substrate/frame/nomination-pools/src/lib.rs` L1450-1511) computes `new_pending_rewards / bonded_points` using the reward account's *current* balance and the bonded pool's *current* total points — it has no concept of which points existed while a given slice of reward was earned. `join()` (L2118-2174) calls `reward_pool.update_records(pool_id, bonded_pool.points, ...)` with the OLD points *before* incrementing `bonded_pool.points` with the joiner's new points; this correctly finalizes any reward that was *already pending in the account before the join*, protecting existing members from dilution on rewards that already landed. However, it provides no protection against rewards that land *after* the join: the joiner's `last_recorded_reward_counter` is simply set to the reward-counter value at join time, and `bonded_pool.points` is permanently increased.

When `payout_stakers`/`payout_stakers_by_page` (`substrate/frame/staking/src/pallet/mod.rs` L1716-1738, callable by "any account... even if it is not one of the stakers") subsequently transfers an entire past-era reward `R` as a raw balance transfer into the pool's reward account, that transfer bypasses `update_records` entirely. The next call to `claim_payout` → `do_reward_payout` (L3524-3571) computes `current_reward_counter` using the pool's *current* `bonded_pool.points` (which now includes the attacker's freshly bonded points `Y`), yielding `pending_rewards ≈ R * Y / (X + Y)` for the attacker — a share of a reward accrued during an era in which the attacker held zero stake. There is no vesting period, minimum dwell time, or era-indexed snapshotting of points to prevent this.

## Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary and amount" invariant for pool payouts: value earned by long-standing nominators over an entire era is redirected to a just-joined attacker. The stolen amount scales with the attacker's capital relative to existing pool points and is transferred to the attacker's *free, immediately spendable* balance via `T::Currency::transfer` in `do_reward_payout`, independent of the pool's/staking's unbonding lock on the attacker's principal. This matches the "duplicate/incorrect settlement or payout" and "theft of value" impact categories for pallet-level fund custody.

## Likelihood Explanation
High. `join`, `payout_stakers`/`payout_stakers_by_page`, and `claim_payout` are all ordinary signed extrinsics with no privileged origin, cooldown, or minimum-dwell requirement, and can be composed atomically in one transaction via `pallet_utility::batch_all`. No front-running, validator collusion, or off-chain infrastructure is required — the attacker can self-trigger the "donation" event (`payout_stakers`) and immediately claim, repeatable every time an unclaimed era-reward exists for a pool's bonded stash.

## Recommendation
Distribute era-based reward inflows landing in a pool's reward account according to a snapshot of points taken before the reward event (e.g., require `update_records` to be invoked, and reward-counter deltas attributed, using the points recorded at the time the corresponding era reward was earned), or impose a minimum bonding dwell time before newly joined points become eligible to share in reward-counter deltas that were already in flight when the join occurred.

## Proof of Concept
1. Pool `P` has existing members with total points `X`; an unclaimed era `E` reward `R` is payable for `P`'s bonded stash via `Staking::payout_stakers(P_bonded_stash, E)`.
2. Attacker submits a single `Utility::batch_all` transaction:
   - `Pools::join(pool_id, huge_amount)` — attacker's points become `Y`, `bonded_pool.points` becomes `X + Y`; `update_records` finalizes any *prior* pending reward using old points `X` (no dilution of past-pending rewards), but the counter reflects no new reward yet since `R` hasn't landed.
   - `Staking::payout_stakers(P_bonded_stash, E)` — transfers `R` into `P`'s reward account as a raw balance transfer, bypassing `update_records`.
   - `Pools::claim_payout()` — `current_reward_counter` recomputes using `bonded_pool.points = X + Y` and the now-inflated balance, giving the attacker `pending_rewards ≈ R * Y / (X + Y)`, paid directly to the attacker's transferable balance.
3. Attacker has captured a share of reward `R` proportional to `Y`, despite holding zero stake during era `E`, diluting the effective share of `R` owed to `P`'s pre-existing members (now computed over `X + Y` instead of `X`).