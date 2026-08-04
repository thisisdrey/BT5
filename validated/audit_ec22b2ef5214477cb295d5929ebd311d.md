### Title
`unbond()` in `pallet-nomination-pools` bundles a mandatory reward transfer with principal unbonding, so a reward-transfer failure blocks a member's access to their principal - ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
`Pallet::<T>::unbond()` claims a member's pending pool rewards via `Self::do_reward_payout(...)` *before* it processes the actual unbonding of the member's principal stake. `do_reward_payout` performs a mandatory `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)` and propagates any error with `?`. If that reward transfer fails, the whole `unbond` extrinsic aborts and the member's principal — which is held separately in the bonded pool account, not the reward account — remains locked, exactly mirroring the `DIAWhitelistedStaking.unstake()` pattern where a reward payout blocks access to principal.

### Finding Description
`unbond()` performs, in order:
1. `reward_pool.update_records(...)`
2. `Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?` — this is the reward claim step
3. Only afterward does it call `T::StakeAdapter::unbond(...)` to actually unbond the member's principal points [1](#0-0) 

`do_reward_payout` computes `pending_rewards` from the pool's internal reward-counter accounting and unconditionally transfers that amount out of the pool's `reward_account()` with `Preservation::Preserve`, using `?` so any transfer failure bubbles straight out of `unbond()`: [2](#0-1) 

The reward account balance is a fungible pool funded by external reward inflows (validator payouts routed to `reward_account()`), not something the unbonding member controls, and it is subject to a frozen existential-deposit reserve (`freeze_pool_deposit`) introduced in the "Fix for Reward Deficit in the pool" change. Because `Preservation::Preserve` errors out if the post-transfer balance would fall below the account's required (frozen) minimum, any drift between the pool's *internally tracked* `pending_rewards` ledger (a pure arithmetic reward-counter calculation) and the *actual spendable* balance of `reward_account()` — caused by rounding in `current_reward_counter`/`pending_rewards` fixed-point math, ED-freeze adjustments via `adjust_pool_deposit`, or a reward account balance that has not yet caught up with commission/claims bookkeeping — makes the transfer fail with an `Err`, which then aborts the entire `unbond()` call and the member's principal-unbonding logic never executes.

This is the same broken invariant as the external report: an operation that should only require moving principal (already fully custodied by the pool logic) is made to depend on an unrelated, externally-fed value transfer (reward payout) that can revert for reasons entirely outside the withdrawing member's control, with no fallback path to withdraw principal alone.

### Impact Explanation
If the reward transfer reverts, a pool member cannot call `unbond()` at all — not just to claim rewards, but to start the unbonding clock on their principal. Since `withdraw_unbonded` also depends on having first passed through `unbond` (points must be moved into `sub_pools`/`unbonding_eras`), the member's principal effectively becomes locked in the bonded pool with no way to exit until the reward-account shortfall is resolved by a third party (pool operator via `bond_extra`/`adjust_pool_deposit`, or by external stakers/commission payees). This is a permanent-fund-lock class impact on staked principal.

### Likelihood Explanation
This requires no malicious actor: it can occur purely from accounting/rounding drift between the reward-counter-based `pending_rewards()` calculation and the reward account's actual reducible balance under `Preservation::Preserve`, or from a pool operator's routine use of `adjust_pool_deposit` shifting the frozen ED requirement upward right before a member unbonds. The existing safeguards (`freeze_pool_deposit`, `Preservation::Preserve`) were added specifically to defend the reward account's ED, but they do so by making the transfer fail loudly rather than gracefully skipping the payout — the exact anti-pattern the external report calls out.

### Recommendation
Decouple the mandatory reward claim from the principal-unbonding path in `unbond()`: either (a) attempt `do_reward_payout` and, on failure, log/skip it (e.g., via a fallible/best-effort claim that does not abort the call) while still proceeding to unbond principal, or (b) require callers to claim rewards separately via `claim_payout` and remove the implicit reward payout from `unbond()` entirely, so a reward-account shortfall can never block a member from starting/completing withdrawal of their own principal.

### Proof of Concept
1. Create a pool with a member `M` holding bonded points and accrued `pending_rewards > 0`.
2. Cause `reward_account()`'s spendable (non-frozen) balance to be less than `pending_rewards` — e.g., have the depositor/root call `adjust_pool_deposit` to raise the frozen ED reserve, or exploit any rounding drift in `current_reward_counter`/`pending_rewards` accounting that overstates claimable amount relative to actual funds credited to `reward_account()`.
3. `M` calls `unbond(origin, M, points)`.
4. `do_reward_payout`'s `T::Currency::transfer(..., Preservation::Preserve)` returns an `Err` (e.g. token `Frozen`/`FundsUnavailable`), which propagates via `?` out of `unbond()`.
5. The entire extrinsic fails: `T::StakeAdapter::unbond(...)` for the principal is never reached, and `M`'s points remain fully active/bonded with no way to begin unbonding until the reward-account shortfall is fixed by someone else. [3](#0-2) [4](#0-3)

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2270-2296)
```rust
			let (mut member, mut bonded_pool, mut reward_pool) =
				Self::get_member_with_pools(&member_account)?;

			bonded_pool.ok_to_unbond_with(&who, &member_account, &member, unbonding_points)?;

			// Claim the the payout prior to unbonding. Once the user is unbonding their points no
			// longer exist in the bonded pool and thus they can no longer claim their payouts. It
			// is not strictly necessary to claim the rewards, but we do it here for UX.
			reward_pool.update_records(
				bonded_pool.id,
				bonded_pool.points,
				bonded_pool.commission.current(),
			)?;
			Self::do_reward_payout(
				&member_account,
				&mut member,
				&mut bonded_pool,
				&mut reward_pool,
			)?;

			let active_era = T::StakeAdapter::current_era();
			let unbond_era = T::StakeAdapter::bonding_duration().saturating_add(active_era);

			// Unbond in the actual underlying nominator.
			let unbonding_balance = bonded_pool.dissolve(unbonding_points);
			T::StakeAdapter::unbond(Pool::from(bonded_pool.bonded_account()), unbonding_balance)?;

```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3524-3563)
```rust
	/// If the member has some rewards, transfer a payout from the reward pool to the member.
	// Emits events and potentially modifies pool state if any arithmetic saturates, but does
	// not persist any of the mutable inputs to storage.
	fn do_reward_payout(
		member_account: &T::AccountId,
		member: &mut PoolMember<T>,
		bonded_pool: &mut BondedPool<T>,
		reward_pool: &mut RewardPool<T>,
	) -> Result<BalanceOf<T>, DispatchError> {
		debug_assert_eq!(member.pool_id, bonded_pool.id);
		debug_assert_eq!(&mut PoolMembers::<T>::get(member_account).unwrap(), member);

		// a member who has no skin in the game anymore cannot claim any rewards.
		ensure!(!member.active_points().is_zero(), Error::<T>::FullyUnbonding);

		let (current_reward_counter, _) = reward_pool.current_reward_counter(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;

		// Determine the pending rewards. In scenarios where commission is 100%, `pending_rewards`
		// will be zero.
		let pending_rewards = member.pending_rewards(current_reward_counter)?;
		if pending_rewards.is_zero() {
			return Ok(pending_rewards);
		}

		// IFF the reward is non-zero alter the member and reward pool info.
		member.last_recorded_reward_counter = current_reward_counter;
		reward_pool.register_claimed_reward(pending_rewards);

		T::Currency::transfer(
			&bonded_pool.reward_account(),
			member_account,
			pending_rewards,
			// defensive: the depositor has put existential deposit into the pool and it stays
			// untouched, reward account shall not die.
			Preservation::Preserve,
		)?;
```
