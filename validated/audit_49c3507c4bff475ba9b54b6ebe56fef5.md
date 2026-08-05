Audit Report

## Title
Reward-per-token accumulator in `pallet-asset-rewards` can be permanently deflated via floor-division rounding while `last_update_block` still advances - (File: `substrate/frame/asset-rewards/src/lib.rs`)

## Summary
`reward_per_token` computes `reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR / total_tokens_staked` using integer (floor) division, and `update_pool_rewards` unconditionally sets `last_update_block` to the current block on every call regardless of whether this delta rounded to zero. Because any call to `stake`, `unstake`, or `harvest_rewards` invokes `update_pool_and_staker_rewards` → `reward_per_token` → `update_pool_rewards`, an account can repeatedly reset `rewardable_blocks_elapsed` to a small window (e.g., 1 block) each time it calls one of these extrinsics, permanently discarding the reward accrual for the skipped time rather than merely delaying it.

## Finding Description
`reward_per_token` at `substrate/frame/asset-rewards/src/lib.rs` (L786-810) computes the elapsed-block reward delta via `pool_info.reward_rate_per_block.ensure_mul(rewardable_blocks_elapsed.into())?.ensure_mul(PRECISION_SCALING_FACTOR.into())?.ensure_div(pool_info.total_tokens_staked)?` and adds it to `reward_per_token_stored`. `update_pool_rewards` (L775-784) then always sets `new_pool_info.last_update_block = T::BlockNumberProvider::current_block_number()`, even when the computed delta was zero due to floor division. `update_pool_and_staker_rewards` (L754-765), called from `stake` (L472 onward) and `unstake` (L513 onward) unconditionally before any staking-amount check, ties these together: every call to these public, unprivileged extrinsics resets the window used to compute `rewardable_blocks_elapsed` on the next call. If `reward_rate_per_block * rewardable_blocks_elapsed * PRECISION_SCALING_FACTOR < total_tokens_staked`, the division truncates to zero and `reward_per_token_stored` is unchanged, but `last_update_block` still advances — so the elapsed time is lost forever rather than accumulating toward a future non-zero delta. This confirmed code path matches the claim exactly.

## Impact Explanation
This affects reward payout/settlement integrity in `pallet-asset-rewards`: stakers can be permanently deprived of rewards that a pool admin funded, because the reward-per-token accumulator can be starved indefinitely while the "last updated" marker keeps advancing, closing the accrual window without applying it. This falls under "public underpriced work that degrades... payout state that fails to conserve value" for reward/pool payout logic, matching the impact gate's allowance for value-conservation failures in reward/pool pallets.

## Likelihood Explanation
No privileged role, validator, governance, or leaked key is needed. Any signed account can call `stake` with a small amount (or `unstake`/`harvest_rewards`) once per block on a pool where `reward_rate_per_block * PRECISION_SCALING_FACTOR < total_tokens_staked` — a realistic parameter combination for pools with low emission rate relative to a large staked-asset supply or high-decimal assets — reliably forcing `rewardable_blocks_elapsed == 1` and the division to floor to zero every time, permanently zeroing reward accrual for the pool's lifetime. This is both easy to trigger adversarially and can occur unintentionally from routine interaction patterns.

## Recommendation
Do not advance `last_update_block` when the computed `reward_per_token` delta rounds to zero, so unapplied elapsed time is preserved for the next update instead of being discarded. Alternatively, substantially increase `PRECISION_SCALING_FACTOR` (e.g., to 1e18-scale) and/or track and carry forward an unapplied remainder across updates so that floor-division loss never permanently erases un-accrued reward time.

## Proof of Concept
1. Admin creates a pool with `reward_rate_per_block = 100` and stakers deposit `total_tokens_staked = 1_000_000`, where `PRECISION_SCALING_FACTOR` (4096) satisfies `100 * 1 * 4096 < 1_000_000`.
2. Any account calls `stake(pool_id, 0)` (or `unstake`/`harvest_rewards`) once per block.
3. Each call computes in `reward_per_token`: `100 * 1 * 4096 / 1_000_000 = 0` (floor), so `reward_per_token_stored` never increases, while `update_pool_rewards` still sets `last_update_block` to the current block.
4. Repeating this every block for the pool's entire lifetime results in `reward_per_token_stored` remaining `0`, so no staker accrues any rewards from `derive_rewards` (L815-824) despite the admin having funded the reward pool.