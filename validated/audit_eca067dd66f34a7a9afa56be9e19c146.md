Audit Report

## Title
`bond_extra`/`claim_payout` force an unrelated reward-payout transfer that reverts and DoSes pool deposits when the reward account is frozen/near-ED - (File: substrate/frame/nomination-pools/src/lib.rs)

## Summary
`Pallet::do_bond_extra` and `Pallet::do_claim_payout` unconditionally call `Self::do_reward_payout`, which performs `T::Currency::transfer(&bonded_pool.reward_account(), member_account, pending_rewards, Preservation::Preserve)`. If the reward account's spendable balance cannot satisfy `Preservation::Preserve` while covering `pending_rewards` (e.g., after an ED increase causing a `Deficit`, as demonstrated by the in-repo test `ed_change_causes_reward_deficit`), this transfer errors and the `?` propagation aborts the entire `bond_extra`/`claim_payout` call, blocking an otherwise valid `FreeBalance` deposit as a side effect of an unrelated forced reward claim.

## Finding Description
`do_bond_extra` (used by both `bond_extra` and `bond_extra_other`) and `do_claim_payout` both call `do_reward_payout` before performing the member's requested action [1](#0-0) . `do_reward_payout` skips the transfer only when `pending_rewards.is_zero()`; otherwise it unconditionally executes the transfer with `Preservation::Preserve` [2](#0-1) . The reward account is frozen at its minimum balance via `freeze_pool_deposit` during pool creation/adjustment [3](#0-2) , and the pallet's own test `ed_change_causes_reward_deficit` demonstrates that a routine ED increase can create a `Deficit` state in the reward pool without any privileged or malicious action, purely as a consequence of runtime parameter changes.

The existing guards in `do_bond_extra` — `T::Filter::contains`, `ClaimPermissions`, and pool existence checks — do not validate reward-account solvency before invoking the forced payout [4](#0-3) .

## Impact Explanation
This matches the accepted "public underpriced work/DoS" pattern only in a narrow sense: it blocks a member's own deposit action due to a coupled, unrelated reward-claim transfer failing. However, this is a self-blocking condition on the caller's own funds/rewards, not a fund-theft, duplicate-settlement, origin-escalation, or permanent third-party fund lock. The affected member can resolve it via `adjust_pool_deposit` (as shown in the same test, called by account `10`), and no other party's funds are put at risk — the deficit is repairable and the impact is limited to a temporary self-inflicted inconvenience for the specific member whose reward account is in deficit, not a chain-halting or fund-loss condition.

## Likelihood Explanation
The deficit condition (ED increase causing reward-account deficit) is a real, testable runtime effect confirmed by `ed_change_causes_reward_deficit`, and any member with nonzero pending rewards would be blocked from calling `bond_extra`/`claim_payout` until the pool depositor (or another account) restores the reward account via `adjust_pool_deposit`. This is deterministic and reproducible given the described preconditions, but requires the somewhat narrow precondition of an ED increase (a rare, network-parameter-level event) coinciding with unclaimed rewards.

## Recommendation
Decouple the mandatory reward-claim from the deposit path for the `FreeBalance` case, or make a failed reward-payout non-fatal (e.g., skip/ignore with an event) when the caller's primary intent is only to add `FreeBalance`, rather than letting `T::Currency::transfer`'s error abort the whole `bond_extra`/`claim_payout` call.

## Proof of Concept
Extend `ed_change_causes_reward_deficit` (`substrate/frame/nomination-pools/src/tests.rs:320-394`): after `ExistentialDeposit::set(50)` produces `reward_imbalance(1) == Deficit(45)`, call `Pools::bond_extra(RuntimeOrigin::signed(11), BondExtra::FreeBalance(10))` for member `11` who has pending rewards of `90`. The forced `do_reward_payout` call inside `do_bond_extra` fails the `Preservation::Preserve` transfer, causing the entire `bond_extra` extrinsic to revert and blocking member 11's valid `FreeBalance` deposit.

### Citations

**File:** substrate/frame/nomination-pools/src/lib.rs (L3547-3563)
```rust
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

**File:** substrate/frame/nomination-pools/src/lib.rs (L3607-3616)
```rust
		// Transfer the minimum balance for the reward account.
		T::Currency::transfer(
			&who,
			&bonded_pool.reward_account(),
			T::Currency::minimum_balance(),
			Preservation::Expendable,
		)?;

		// Restrict reward account balance from going below ED.
		Self::freeze_pool_deposit(&bonded_pool.reward_account())?;
```

**File:** substrate/frame/nomination-pools/src/lib.rs (L3657-3670)
```rust
		// ensure account is not restricted from joining the pool.
		ensure!(!T::Filter::contains(&member_account), Error::<T>::Restricted);

		if signer != member_account {
			ensure!(
				ClaimPermissions::<T>::get(&member_account).can_bond_extra(),
				Error::<T>::DoesNotHavePermission
			);
			ensure!(extra == BondExtra::Rewards, Error::<T>::BondExtraRestricted);
		}

		let (mut member, mut bonded_pool, mut reward_pool) =
			Self::get_member_with_pools(&member_account)?;

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
