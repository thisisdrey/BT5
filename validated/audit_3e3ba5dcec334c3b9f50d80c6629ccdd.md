## Analog Identified: Nomination pool exit is fully gated on an internal reward-payout call that can fail and permanently trap staked funds

### Title
Nomination pool `unbond`/`claim_payout` hard-couple to `do_reward_payout`, letting a failed reward transfer permanently block a member from exiting the pool - (File: `substrate/frame/nomination-pools/src/lib.rs`)

### Summary
The external report describes `OgvStaking`'s core `stake`/`unstake`/`extend` functions being strongly coupled to `RewardsSource.collectRewards`: any revert in the reward-collection dependency blocks the staking/governance functionality itself. The same coupling pattern exists in `pallet-nomination-pools`: the public extrinsics `unbond` and `claim_payout` unconditionally propagate the result of the internal `do_reward_payout` call with `?`. If the reward-account transfer inside `do_reward_payout` fails, the whole `unbond`/`claim_payout` call fails, and the pool member cannot exit or retrieve their bonded funds.

### Finding Description
`Call::unbond` (which lets a member start withdrawing their staked funds) claims the pending reward "for UX" before unbonding, and the error is directly propagated: [1](#0-0) 

That call goes into `do_reward_payout`, whose last step performs a `T::Currency::transfer` from the (shared) `reward_account()` to the member, using `Preservation::Preserve`: [2](#0-1) 

`claim_payout` follows the identical pattern via `do_claim_payout -> do_reward_payout`: [3](#0-2) 

The reward account's actual free balance is a single pooled pot shared by every member's accounting entitlement (`current_reward_counter`, `pending_rewards`) plus any pending commission. The pallet's own `try-state` invariant check acknowledges that the sum of members' bookkept pending rewards can, due to rounding in the reward-counter math, exceed the leftover balance actually held in the reward account: [4](#0-3) 

When that dust/rounding deficit materializes for a given member's turn, `T::Currency::transfer(..., Preservation::Preserve)` in `do_reward_payout` returns an `Err` (insufficient funds to honor the requested amount while preserving the account), and that `Err` is not caught anywhere — it propagates straight through `do_reward_payout(...)?` in `unbond` (and in `claim_payout`), aborting the entire extrinsic. Because `unbond` is the only entry point that reduces a member's active points (and it always tries to flush pending rewards first "since not doing so would mean some rewards would be forfeited," per the doc comment at lines 2224-2227), a member stuck behind this failing transfer has no way to reduce their `pending_rewards` to zero and no way to unbond — the same "silent revert blocks core functionality" pattern as the reported `OgvStaking`/`RewardsSource` coupling.

### Impact Explanation
This matches the "permanent user-fund ... lock" category in the impact gate: an unprivileged pool member's bonded stake becomes permanently unbondable/unwithdrawable because a coupled internal reward-transfer operation fails and that failure is not isolated from the member-facing exit path. Unlike the Ethereum report's `stake`/`unstake`, there is no `try/catch`-equivalent wrapper (no `.ok()`, no best-effort fallback) around the internal reward call in these extrinsics — the failure directly and fully blocks the wrapping public call.

### Likelihood Explanation
This does not require a malicious peer, validator, collator, or governance/admin actor — it is a structural coupling flaw between accounting bookkeeping (`current_reward_counter`/`pending_rewards`, which is per-member floating-point-like `Perbill`-driven arithmetic) and the pot's actual on-chain balance, which the codebase's own try-runtime check documents as capable of diverging ("this is most likely due to an old bug ... we warn about this in try-runtime checks but do not panic"). Any operational path that produces such a deficit (rounding across many small deposits/claims, or commission being paid out of the same pot) can trigger this for an arbitrary, unprivileged member attempting `unbond`/`claim_payout`.

### Recommendation
Decouple the incidental reward flush inside `unbond`/`claim_payout` from the primary state transition, mirroring the report's suggested fix:
- Do not propagate the `do_reward_payout` error with `?` inside `unbond`; instead, treat a failed reward transfer as best-effort (e.g., log/emit an event and continue with the unbond), so a member is never trapped in the pool solely because their small reward transfer failed.
- Alternatively, cap the transferred amount to `min(pending_rewards, actual reward-account spendable balance)` so the transfer can never fail due to insufficient reward-account balance, and reconcile any shortfall via the existing dust-accounting mechanism instead of aborting the extrinsic.

### Proof of Concept
1. Set up a pool with multiple members and repeated small `deposit_rewards`/`claim_payout` cycles so that `RewardPool::current_reward_counter` accumulates fractional (`Perbill`)-based rounding, per the mechanism flagged in the pallet's own `try-runtime` check at lines 3998-4014 of `substrate/frame/nomination-pools/src/lib.rs`.
2. Once the reward account's actual free balance (`RewardPool::current_balance`) drops slightly below the sum of members' bookkept `pending_rewards`, have a member call `Pools::unbond(origin, member_account, unbonding_points)`.
3. Inside `unbond`, `Self::do_reward_payout(...)?` (line 2283) invokes `T::Currency::transfer(&reward_account(), member_account, pending_rewards, Preservation::Preserve)` (line 3556). Because the reward account cannot honor the transfer while being preserved, this call returns `Err`, which propagates via `?` and aborts the whole `unbond` extrinsic (`DispatchResult`).
4. The member cannot unbond (nor `claim_payout`, which hits the identical failure), permanently locking their staked funds in the pool as long as the deficit condition persists — the exact coupling failure mode described in the external report, now expressed as a fund-lock instead of a disabled-staking DoS.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L2275-2288)
```rust
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
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3552-3563)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3763-3770)
```rust
		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?;

		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);
		Ok(())
	}
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3998-4014)
```rust
		RewardPools::<T>::iter_keys().try_for_each(|id| -> Result<(), TryRuntimeError> {
			// the sum of the pending rewards must be less than the leftover balance. Since the
			// reward math rounds down, we might accumulate some dust here.
			let pending_rewards_lt_leftover_bal = RewardPool::<T>::current_balance(id) >=
				pools_members_pending_rewards.get(&id).copied().unwrap_or_default();

			// If this happens, this is most likely due to an old bug and not a recent code change.
			// We warn about this in try-runtime checks but do not panic.
			if !pending_rewards_lt_leftover_bal {
				log!(
					warn,
					"pool {:?}, sum pending rewards = {:?}, remaining balance = {:?}",
					id,
					pools_members_pending_rewards.get(&id),
					RewardPool::<T>::current_balance(id)
				);
			}
```
