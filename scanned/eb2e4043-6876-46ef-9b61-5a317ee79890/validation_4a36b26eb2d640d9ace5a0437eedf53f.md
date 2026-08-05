### Title
`harvest_rewards` never persists the updated `PoolInfo` reward checkpoint, allowing repeated over-accrual/duplicate reward payout in `pallet-asset-rewards` - (`substrate/frame/asset-rewards/src/lib.rs`)

### Summary
`Pallet::harvest_rewards` computes an updated `pool_info` (with an advanced `reward_per_token_stored` and `last_update_block`) via `update_pool_and_staker_rewards`, uses it to compute the staker's payout, and transfers the reward — but never writes the updated `pool_info` back into the `Pools<T>` storage map. Every sibling extrinsic that mutates pool/staker state (`stake`, `unstake`) does call `Pools::<T>::insert(pool_id, pool_info)` after the same helper call, but `harvest_rewards` omits it. This is the same bug class as the reported Move `withdraw_reserve_amount` issue: state that is withdrawn/paid out is not reflected back into the persistent “total accounted” record, producing systematic accounting drift.

### Finding Description
`update_pool_and_staker_rewards` (`substrate/frame/asset-rewards/src/lib.rs:754-765`) is a pure function: it derives `reward_per_token` from the pool's `reward_rate_per_block`, `total_tokens_staked`, `last_update_block`, and the current block, then returns a *new* `pool_info` with `last_update_block` advanced to `now` and `reward_per_token_stored` bumped to the newly computed value, along with a `new_staker_info` whose `reward_per_token_paid` is checkpointed to that same new value.

Compare the three call sites:
- `stake` (`lib.rs:472-502`): calls the helper, then `Pools::<T>::insert(pool_id, pool_info);` — persists the checkpoint.
- `unstake` (`lib.rs:513-560`): calls the helper, then `Pools::<T>::insert(pool_id, pool_info);` — persists the checkpoint.
- `harvest_rewards` (`lib.rs:569-615`): calls the helper, uses `pool_info.reward_asset_id`/`pool_info.account` for the transfer, but **never** calls `Pools::<T>::insert(pool_id, pool_info)`. The advanced `reward_per_token_stored`/`last_update_block` exist only in the local variable and are discarded when the extrinsic returns.

`reward_per_token` (`lib.rs:786-810`) computes newly accrued reward strictly as a function of `pool_info.last_update_block` (blocks elapsed since last checkpoint) and `pool_info.total_tokens_staked`. Because `harvest_rewards` never advances `last_update_block` in storage, the next call to `reward_per_token` (from any staker's `stake`/`unstake`/`harvest_rewards`) recomputes `rewardable_blocks_elapsed` starting from the *stale* `last_update_block` that predates the harvest, i.e. it re-counts the same block interval that was already paid out to the harvesting staker. This directly inflates `reward_per_token_stored` for the whole pool, which is shared across all stakers (`staker_info.rewards` is derived from `reward_per_token_stored − reward_per_token_paid`, per `derive_rewards`). The staker who just harvested has their own `reward_per_token_paid` updated locally and persisted via `PoolStakers::<T>::insert`, so *that* staker is not double-paid on the next call — but every *other* staker in the pool now accrues extra rewards for a block window that was already funded once, and the harvesting staker can trigger this drift repeatedly by calling `harvest_rewards` at each block, indefinitely re-extending the "unadvanced" window relative to storage.

No existing guard catches this: there is no invariant check comparing pool reward-asset holdings vs. the sum of stakers' entitlements (unlike, e.g., `pallet_treasury`'s `pot()` checks or `reserve::check_stats_integrity` in the external report). The transfer in `harvest_rewards` succeeds as long as `pool_info.account` has a sufficient reward-asset balance, and `Preservation::Expendable` allows draining the account.

### Impact Explanation
This is a public, unprivileged-caller reachable path (`harvest_rewards` is callable by the staker themselves at any time while the pool is active) that produces silent over-accrual of a shared reward-per-token accumulator. Over repeated calls, this results in more reward-asset value being computed as owed to stakers than the pool's `reward_rate_per_block × elapsed_blocks` design intends, i.e. it can drain the pool's `pool_info.account` reward-asset balance faster than intended, effectively letting one or more stakers extract reward tokens that were not legitimately accrued — a form of unbacked/over-issued payout of the reward asset out of the pool account. This matches the "theft or unbacked mint" / "duplicate settlement or payout" impact class for the live-scope program (accounting divergence causing wrongful fund transfer from a pallet-controlled account).

### Likelihood Explanation
High likelihood of being triggered unintentionally (any staker calling `harvest_rewards` normally hits this path — there is no special condition required), and it is also directly exploitable by a staker who wants to accelerate reward drift: repeatedly calling `harvest_rewards` (once per block or with minimal staked amount) keeps `Pools::last_update_block` frozen at (or before) the block of their first harvest while other stakers' derived rewards keep counting elapsed blocks from that same stale checkpoint, inflating the shared `reward_per_token_stored` faster than the configured `reward_rate_per_block` intends. No privileged actor, governance action, or malicious external party (peer/relayer/validator) is required — a normal signed extrinsic caller is sufficient.

### Recommendation
In `harvest_rewards`, after computing the updated `pool_info` via `update_pool_and_staker_rewards`, persist it exactly as `stake`/`unstake` do:
```rust
let (pool_info, mut staker_info) =
    Self::update_pool_and_staker_rewards(&pool_info, &staker_info)?;
Pools::<T>::insert(pool_id, pool_info.clone()); // <-- missing checkpoint write
```
placed before (or immediately after) the `T::Assets::transfer` call, so `last_update_block`/`reward_per_token_stored` are checkpointed in storage the same way they are for `stake` and `unstake`.

### Proof of Concept
1. Create a reward pool with `reward_rate_per_block = R`, `total_tokens_staked = S` (via `create_pool`/`stake` from staker A and staker B).
2. At block `N`, staker A calls `harvest_rewards(pool_id, None)`.
   - `update_pool_and_staker_rewards` computes `reward_per_token_stored' = reward_per_token_stored + R*(N - last_update_block)/S` and returns a `pool_info` with `last_update_block = N`.
   - Staker A's `PoolStakers` entry is updated with `reward_per_token_paid = reward_per_token_stored'`, but `Pools::<T>::get(pool_id).last_update_block` remains the *old* value (pre-harvest), since `Pools::<T>::insert` is never called.
3. At a later block `N + K`, staker B calls `harvest_rewards` (or `stake`/`unstake`).
   - `reward_per_token` recomputes `rewardable_blocks_elapsed` as `(N + K) - last_update_block` where `last_update_block` is still the stale pre-harvest value, not `N`. This re-includes the `[last_update_block, N]` interval that was already paid out to staker A in step 2, inflating `reward_per_token_stored` beyond `R * (N + K - original_last_update_block)/S`.
   - Staker B's rewards (and any other staker who has not yet harvested) are computed from this inflated `reward_per_token_stored`, receiving more reward-asset than `reward_rate_per_block` accounts for.
4. Repeating step 2 (staker A calling `harvest_rewards` every block) keeps `Pools::last_update_block` pinned to a stale value indefinitely, continuously inflating the shared accumulator relative to the pool's real reward budget, and can be observed by comparing `pool_info.account`'s reward-asset balance depletion rate against `reward_rate_per_block`.

*Note: this analysis is based on static code review of `substrate/frame/asset-rewards/src/lib.rs`; I was unable to execute the pallet's test suite to empirically confirm the numeric over-accrual in a running node within this session — this should be validated with a runtime test asserting `Pools::<T>::get(pool_id).last_update_block` and reward totals across a `harvest_rewards` → `stake`/`unstake` sequence.*