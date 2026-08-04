## Analysis

The external report's core invariant is: **burning/destroying the record that gates reward-claim eligibility, without first settling pending rewards tied to that record, permanently locks the reward**. Claiming requires the record to still exist (`isApprovedOrOwner`/ownership check), but the withdrawal path destroys the record as its last step.

The exact same pattern exists in `pallet-nomination-pools`.

### Title
Full withdrawal in `withdraw_unbonded` deletes `PoolMembers` record without settling pending pool rewards, permanently locking accrued rewards - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`Pallet::withdraw_unbonded` in `substrate/frame/nomination-pools/src/lib.rs` removes a member's `PoolMembers` storage entry once their bonded `points` reach zero [1](#0-0) . This removal happens without first invoking the reward-payout logic that pays out the member's outstanding share of `RewardPools` based on their reward counter. `claim_payout` (the only extrinsic that can release pool rewards to a member) requires `PoolMembers::<T>::get(&who)` to succeed [2](#0-1) , exactly analogous to the `isApprovedOrOwner`/ownership gate in the VotingEscrow report. Once the member record is deleted by `withdraw_unbonded`, there is no code path left to retrieve any reward accrued but not yet claimed at that point.

### Finding Description
`withdraw_unbonded` computes `withdrawn_points` from `member.withdraw_unlocked(active_era)`, moves the unbonded balance out via `T::StakeAdapter::member_withdraw`, and then, when `member.total_points().is_zero()`, unconditionally clears `ClaimPermissions` and calls `PoolMembers::<T>::remove(&member_account)` [1](#0-0) . Nowhere in this branch is `do_reward_payout`/`claim_payout` invoked to settle the member's accumulated reward-counter delta against `RewardPools` before the record disappears.

The reward system (`RewardPools`, per-member `last_recorded_reward_counter`) tracks each member's unclaimed share proportional to their historical points; it is only realized/paid when `claim_payout` executes, and `claim_payout` unconditionally requires the `PoolMembers` entry to exist as the source of the member's `pool_id`, `points`, and `last_recorded_reward_counter` [3](#0-2) . A member who fully unbonds and calls `withdraw_unbonded` (or has it triggered permissionlessly against them once the pool is `Destroying`, per `bonded_pool.ok_to_withdraw_unbonded_with`) loses this record. Any reward accrued between their last `claim_payout` call and the moment their points hit zero — which is entirely normal, since unbonding takes multiple eras and rewards continue accumulating on the pool for members who haven't fully exited — becomes permanently unreachable: no extrinsic exists to look up a nonexistent `PoolMembers` entry to pay it out. This exactly parallels burning the VotingEscrow NFT before `getReward()` can be called.

### Impact Explanation
Pool member funds (bribe/reward analog: pool staking rewards) become permanently stranded in the pool's reward account, effectively locked rather than settled to the rightful beneficiary — matching the required-impact class "permanent user-fund ... lock" and "duplicate settlement or payout" avoidance failure (settlement never completes exactly once for the member). Since `withdraw_unbonded` can be dispatched permissionlessly against a member once the pool enters `PoolState::Destroying` (documented as a permissionless-dispatch condition) [4](#0-3) , an unrelated caller can trigger the removal of another member's record and thereby strand that member's unclaimed reward without any admin/governance/malicious-peer precondition.

### Likelihood Explanation
Any pool member who does not call `claim_payout` immediately before their final `withdraw_unbonded` call — a common, unprivileged sequence of actions (unbond → wait bonding duration → withdraw) — will hit this path whenever the pool continues to receive rewards during the unbonding window or the member simply forgets to claim first. No special conditions, governance, or malicious actor are required; it is a straightforward ordering issue on public extrinsics available to every pool member.

### Recommendation
Before the `member.total_points().is_zero()` branch removes `PoolMembers::<T>::remove(&member_account)` in `withdraw_unbonded`, call the internal reward-payout routine to settle any outstanding reward for that member against `RewardPools` (mirroring what `claim_payout` does), or refuse to fully remove the record until the pending reward-counter delta has been paid out/transferred to the member's account.

### Proof of Concept
1. Member `M` joins a pool and pool commission/rewards accrue over several eras via `deposit_rewards`-style external reward transfers to the pool's reward account.
2. `M` calls `unbond` for their full balance, then waits the bonding duration without calling `claim_payout` (rewards continue to accrue on the pool in the interim, e.g. other transfers to the reward account, or the "rounded down" behavior demonstrated by test `rewards_are_rounded_down_depositor_collects_them` at `substrate/frame/nomination-pools/src/tests.rs:2489-2551`, which shows rewards distributed alongside withdrawal flows).
3. `M` (or, once the pool is `Destroying`, any third party) calls `withdraw_unbonded(member_account = M, ...)`.
4. Because `member.total_points()` is now zero, the call path at `substrate/frame/nomination-pools/src/lib.rs:2514-2519` removes `PoolMembers::<T>` for `M` without paying out `M`'s accrued reward-counter delta.
5. `M` subsequently calls `claim_payout`; it fails at `PoolMembers::<T>::get(&member_account).ok_or(Error::<T>::PoolMemberNotFound)?` (line 2409), permanently denying access to the reward that had accrued but was never settled.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2372-2379)
```rust
		/// Under certain conditions, this call can be dispatched permissionlessly (i.e. by any
		/// account).
		///
		/// # Conditions for a permissionless dispatch
		///
		/// * The pool is in destroy mode and the target is not the depositor.
		/// * The target is the depositor and they are the only member in the sub pools.
		/// * The pool is blocked and the caller is either the root or bouncer.
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2408-2410)
```rust
			let mut member =
				PoolMembers::<T>::get(&member_account).ok_or(Error::<T>::PoolMemberNotFound)?;
			let active_era = T::StakeAdapter::current_era();
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2514-2519)
```rust
			let post_info_weight = if member.total_points().is_zero() {
				// remove any `ClaimPermission` associated with the member.
				ClaimPermissions::<T>::remove(&member_account);

				// member being reaped.
				PoolMembers::<T>::remove(&member_account);
```
