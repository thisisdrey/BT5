Audit Report

## Title
`bond_extra`/`claim_payout` force an unrelated reward-payout transfer that reverts and DoSes pool deposits when the reward account is frozen/near-ED - (File: substrate/frame/nomination-pools/src/lib.rs)

## Summary
`Pallet::do_bond_extra` unconditionally calls `Self::do_reward_payout` before executing the member's requested bond action, and `do_reward_payout` performs `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)`. This transfer is unrelated to the member's `FreeBalance` deposit intent but its failure (via the `?` operator) aborts the entire `bond_extra` call, coupling an unrelated reward-claim step to a deposit action.

## Finding Description
`do_bond_extra` calls `do_reward_payout` unconditionally for any `BondExtra` variant, including `FreeBalance`: [1](#0-0) 

`do_reward_payout` only skips the transfer when `pending_rewards.is_zero()`; otherwise it performs `T::Currency::transfer(..., Preservation::Preserve)?`, whose failure propagates directly out of `do_bond_extra`: [2](#0-1) 

The pallet's own test suite demonstrates that the reward pool can enter a `Deficit` state purely from an ordinary ED increase (`ed_change_causes_reward_deficit`), and that this deficit persists across subsequent pool operations (member joins) until manually repaired via `adjust_pool_deposit`: [3](#0-2) 

The existing guards in `do_bond_extra` (filter check, claim-permission check, pool lookup) do not validate reward-account solvency before invoking the forced payout.

However, I was not able to fully verify within the available context whether the `Deficit` value reported by the test's `reward_imbalance` helper (a virtual/accounting imbalance between the reward pool's ledger and the currently required ED) actually corresponds to a real insufficient balance in the reward account that would cause `T::Currency::transfer(..., Preservation::Preserve)` to fail for the specific `pending_rewards` amount owed to a given member. The test `ed_change_causes_reward_deficit` only exercises `join` and `deposit_rewards`/`adjust_pool_deposit`; it does not itself call `bond_extra` or `claim_payout` and assert a transfer failure. Confirming the claim's core mechanism — that this "deficit" state deterministically causes the `Preservation::Preserve` transfer inside `do_reward_payout` to return `Err` for a normal member with non-zero pending rewards — would require constructing and running the exact extended test proposed in the PoC, which is beyond what I can confirm through static reading alone.

## Impact Explanation
If the mechanism holds as described, the impact matches the "public underpriced work / DoS on deposits" category: normal, permissionless `bond_extra`/`claim_payout` calls would be blocked as a side effect of an unrelated, coupled reward-claim transfer, freezing legitimate stake/reward flows for affected members until the reward account deficit is repaired via `adjust_pool_deposit` (a permissionless but manual action). This is not a fund-loss or theft scenario; it is an availability/DoS issue affecting individual members' ability to deposit or compound stake.

## Likelihood Explanation
The triggering condition (a runtime-parameter ED increase producing a reward-pool deficit) is demonstrated in-repo as achievable through ordinary chain operation, not requiring malicious or privileged action, which aligns with the required "no malicious admin/peer" condition for a valid finding.

## Recommendation
Decouple the mandatory reward-claim step from the `FreeBalance` bonding path in `do_bond_extra`, or make a failed reward payout non-fatal to the deposit when the caller's intent is solely to add `FreeBalance` (e.g., skip/soft-fail the payout with an event rather than propagating the error via `?`). Alternatively, ensure the reward account is topped up automatically before attempting the pending payout rather than letting `T::Currency::transfer` fail and abort the whole extrinsic.

## Proof of Concept
Extend `ed_change_causes_reward_deficit` (`substrate/frame/nomination-pools/src/tests.rs:320-394`): after `ExistentialDeposit::set(50)` produces `reward_imbalance(1) == Deficit(45)`, call `Pools::bond_extra(RuntimeOrigin::signed(11), BondExtra::FreeBalance(10))` for member `11` (who has pending rewards of `90`), and assert whether the call reverts due to the `T::Currency::transfer` failure inside `do_reward_payout`. This test needs to actually be run to confirm the transfer fails under these conditions, which was not verified in this review.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L3545-3563)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3671-3692)
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

		let (points_issued, bonded) = match extra {
			BondExtra::FreeBalance(amount) => {
				(bonded_pool.try_bond_funds(&member_account, amount, BondType::Extra)?, amount)
			},
			BondExtra::Rewards => {
				(bonded_pool.try_bond_funds(&member_account, claimed, BondType::Extra)?, claimed)
			},
		};
```

**File:** substrate/frame/nomination-pools/src/tests.rs (L320-394)
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
			assert_eq!(reward_imbalance(1), Surplus(0));

			// 12 joins the pool.
			Currency::set_balance(&12, 500);
			assert_ok!(Pools::join(RuntimeOrigin::signed(12), 100, 1));

			// Current reward balance is committed to last recorded reward counter of
			// the pool before the increase in ED.
			let bonded_pool = BondedPools::<Runtime>::get(1).unwrap();
			let reward_pool = RewardPools::<Runtime>::get(1).unwrap();
			assert_eq!(
				reward_pool.last_recorded_reward_counter,
				reward_pool
					.current_reward_counter(1, bonded_pool.points, Perbill::zero())
					.unwrap()
					.0
			);

			// reward pool before ED increase and reward counter getting committed.
			let reward_pool_1 = RewardPools::<Runtime>::get(1).unwrap();

			// increase ED from 5 to 50
			ExistentialDeposit::set(50);

			// There is now an expected deficit of ed_diff
			assert_eq!(reward_imbalance(1), Deficit(45));

			// 13 joins the pool which commits the reward counter to reward pool.
			Currency::set_balance(&13, 500);
			assert_ok!(Pools::join(RuntimeOrigin::signed(13), 100, 1));

			// still a deficit
			assert_eq!(reward_imbalance(1), Deficit(45));

			// reward pool after ED increase
			let reward_pool_2 = RewardPools::<Runtime>::get(1).unwrap();

			// last recorded total payout does not decrease even as ED increases.
			assert_eq!(
				reward_pool_1.last_recorded_total_payouts,
				reward_pool_2.last_recorded_total_payouts
			);

			// Topping up pool decreases deficit
			deposit_rewards(10);
			assert_eq!(reward_imbalance(1), Deficit(35));

			// top up the pool to remove the deficit
			deposit_rewards(35);
			// No deficit anymore
			assert_eq!(reward_imbalance(1), Surplus(0));

			// fix the ed deficit
			assert_ok!(Currency::mint_into(&10, 45));
			assert_ok!(Pools::adjust_pool_deposit(RuntimeOrigin::signed(10), 1));
		});
	}
```
