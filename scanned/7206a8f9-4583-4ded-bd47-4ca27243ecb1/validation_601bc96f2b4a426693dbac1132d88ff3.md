## Title
`do_reward_payout` in `pallet-nomination-pools` computes a claimable amount from a points-based counter without validating it against the reward account's actual spendable balance, causing `claim_payout`, `unbond`, and `bond_extra` to fail unexpectedly — ([File: substrate/frame/nomination-pools/src/lib.rs])

### Summary
The Sandclock report's root cause is a "phantom balance" bug: `totalUnderlying()` counts assets that are not actually held by the contract, so functions that pay out based on this accounted value fail when the real balance is insufficient. The local analog is in `pallet-nomination-pools`'s reward-payout path: `do_reward_payout` derives `pending_rewards` purely from an internal points/reward-counter accounting model (`RewardPool::current_reward_counter` / `PoolMember::pending_rewards`) and then unconditionally attempts `T::Currency::transfer(&reward_account, member_account, pending_rewards, Preservation::Preserve)` [1](#0-0) . Nothing in this path caps `pending_rewards` at the reward account's actual transferable balance before attempting the transfer.

### Finding Description
`do_reward_payout` computes `pending_rewards` from `member.pending_rewards(current_reward_counter)`, which is a purely arithmetic quantity derived from points and a running reward counter, not from the reward account's live balance [2](#0-1) . It then calls `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)` without first checking whether the reward account actually holds at least `pending_rewards` above its existential deposit [3](#0-2) .

The codebase itself documents a known mechanism by which the accounted "pending rewards" can exceed the account's real spendable balance: an existential-deposit increase creates a "reward deficit," and the code comment explicitly acknowledges the reward account "might go in deficit temporarily" [4](#0-3) . Tests such as `ed_change_causes_reward_deficit` and `ed_adjust_fixes_reward_deficit` confirm this deficit state is reachable and that `reward_imbalance()` can report `Deficit(45)` while pending rewards for members remain computed as if fully backed [5](#0-4) .

`do_reward_payout` is invoked from three call paths that any pool member can trigger unprivileged: `claim_payout` → `do_claim_payout` [6](#0-5) , `unbond` [7](#0-6) , and `bond_extra`/`bond_extra_other` (`do_bond_extra`) [8](#0-7) . In all three, if the reward account balance can't cover the accounted `pending_rewards` while preserving ED, the `T::Currency::transfer` returns `Err`, and the whole extrinsic — `claim_payout`, `unbond`, or `bond_extra` — fails with an unexpected error, exactly mirroring the Sandclock pattern where `withdraw`/`claimYield`/`unsponsor` fail because the accounted value (`totalUnderlying()`) doesn't match actual available balance.

Compare this to `withdraw_unbonded`, where the analogous risk was explicitly patched: the balance to release is clamped via `.min(T::StakeAdapter::transferable_balance(...))` specifically to avoid attempting to move funds that don't exist [9](#0-8) . No equivalent clamp exists in `do_reward_payout`.

### Impact Explanation
When the reward account's real balance falls short of the accounted `pending_rewards` (e.g., due to ED increases affecting minimum-balance requirements, or cumulative rounding in point/balance ratio math across many claims from many members), `claim_payout`, `unbond`, and `bond_extra` can revert unexpectedly for pool members with legitimately-earned, correctly-tracked reward entitlements. This blocks reward payouts and can also block unbonding (since `unbond` calls `do_reward_payout` before proceeding), effectively locking a member's exit from the pool until the deficit is resolved — a fund-availability degradation aligned with the "permanent user-fund... lock" impact category. `pallet-nomination-pools` is used broadly across relay chain and parachain runtimes for staking, so this affects a core, widely-used pallet.

### Likelihood Explanation
The ED-driven deficit path requires a runtime-parameter change (`ExistentialDeposit`) rather than an unprivileged transaction, so on its own it is a rarer, indirectly-triggered condition (governance/ED changes are out of scope per the task's exclusion criteria). However, the structural weakness — computing a payout amount from internal accounting without checking it against the account's actual transferable balance before transfer — remains present in `do_reward_payout` regardless of trigger, and is inconsistent with the defensive `.min(transferable_balance(...))` pattern already applied elsewhere in the same pallet (`withdraw_unbonded`). I was not able to fully verify within the available iterations whether rounding in `RewardPool::current_reward_counter`/`point_to_balance` math alone (without any ED change) can independently produce a deficit large enough to fail a transfer in typical operation; this would require deeper analysis of the fixed-point reward-counter arithmetic across many join/unbond/claim sequences.

### Recommendation
In `do_reward_payout`, before calling `T::Currency::transfer`, clamp `pending_rewards` to the reward account's actual transferable balance (analogous to the `.min(transferable_balance(...))` guard used in `withdraw_unbonded`), and persist/track any shortfall so it can be paid out later once the reward account is topped up, rather than letting the whole extrinsic (`claim_payout`, `unbond`, `bond_extra`) fail.

### Proof of Concept
1. Create a pool and add members as in `ed_change_causes_reward_deficit` [10](#0-9) .
2. Deposit rewards so members accrue `pending_rewards` proportional to points.
3. Increase `ExistentialDeposit`, producing `reward_imbalance(1) == Deficit(45)` [11](#0-10)  — the reward account's real spendable balance is now less than the sum of members' accounted `pending_rewards`.
4. A member calls `Pools::claim_payout` (or `unbond`, or `bond_extra_other` with `BondExtra::Rewards`). `do_reward_payout` computes `pending_rewards` from the counter and calls `T::Currency::transfer(&reward_account, member, pending_rewards, Preservation::Preserve)` [3](#0-2) ; if the reward account cannot cover `pending_rewards` while keeping its frozen ED, the transfer fails and the whole extrinsic returns an error, blocking the member's claim/unbond/bond-extra despite their reward entitlement being correctly recorded on-chain.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L1438-1443)
```rust
		// An increase in ED could cause `last_recorded_total_payouts` to decrease but we should not
		// allow that to happen since an already paid out reward cannot decrease. The reward account
		// might go in deficit temporarily in this exceptional case but it will be corrected once
		// new rewards are added to the pool.
		self.last_recorded_total_payouts =
			self.last_recorded_total_payouts.max(last_recorded_total_payouts);
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L2277-2288)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L2486-2496)
```rust
				// A call to this transaction may cause the pool's stash to get dusted. If this
				// happens before the last member has withdrawn, then all subsequent withdraws will
				// be 0. However the unbond pools do no get updated to reflect this. In the
				// aforementioned scenario, this check ensures we don't try to withdraw funds that
				// don't exist. This check is also defensive in cases where the unbond pool does not
				// update its balance (e.g. a bug in the slashing hook.) We gracefully proceed in
				// order to ensure members can leave the pool and it can be destroyed.
				.min(T::StakeAdapter::transferable_balance(
					Pool::from(bonded_pool.bonded_account()),
					Member::from(member_account.clone()),
				));
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3539-3563)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3671-3683)
```rust
		// payout related stuff: we must claim the payouts, and updated recorded payout data
		// before updating the bonded pool points, similar to that of `join` transaction.
		reward_pool.update_records(
			bonded_pool.id,
			bonded_pool.points,
			bonded_pool.commission.current(),
		)?;
		let claimed = Self::do_reward_payout(
			&member_account,
			&mut member,
			&mut bonded_pool,
			&mut reward_pool,
		)?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3753-3769)
```rust
	pub(crate) fn do_claim_payout(
		signer: T::AccountId,
		member_account: T::AccountId,
	) -> DispatchResult {
		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_claim_payout(),
				Error::<T>::DoesNotHavePermission
			);
		}
		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

		Self::do_reward_payout(&member_account, &mut member, &mut bonded_pool, &mut reward_pool)?;

		Self::put_member_with_pools(&member_account, member, bonded_pool, reward_pool);
		Ok(())
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L320-337)
```rust
	fn ed_change_causes_reward_deficit() {
		ExtBuilder::default().max_members_per_pool(Some(5)).build_and_execute(|| {
			// original ED
			ExistentialDeposit::set(5);

			// 11 joins the pool
			Currency::set_balance(&11, 500);
			assert_ok!(Pools::join(RuntimeOrigin::signed(11), 90, 1));

			// new delegator does not have any pending rewards
			assert_eq!(pending_rewards_for_delegator(11), 0);

			// give the pool some rewards
			deposit_rewards(100);

			// all existing delegator has pending rewards
			assert_eq!(pending_rewards_for_delegator(11), 90);
			assert_eq!(pending_rewards_for_delegator(10), 10);
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L359-370)
```rust
			// increase ED from 5 to 50
			ExistentialDeposit::set(50);

			// There is now an expected deficit of ed_diff
			assert_eq!(reward_imbalance(1), Deficit(45));

			// 13 joins the pool which commits the reward counter to reward pool.
			Currency::set_balance(&13, 500);
			assert_ok!(Pools::join(RuntimeOrigin::signed(13), 100, 1));

			// still a deficit
			assert_eq!(reward_imbalance(1), Deficit(45));
```
