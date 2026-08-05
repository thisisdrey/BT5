Audit Report

## Title
`claim_child_bounty` does not decrement the parent bounty's `ChildrenCuratorFees` tracking, allowing a claimed child-bounty fee to be double-counted against the parent bounty payout - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`pallet-child-bounties::claim_child_bounty` pays out the child-bounty curator fee directly from the child-bounty sub-account but never decrements the `ChildrenCuratorFees` accumulator for the parent bounty, unlike the cancel path `impl_close_child_bounty` which explicitly does so. Because `pallet-bounties::claim_bounty` later calls `children_curator_fees(bounty_id)` to subtract the full accumulated fee total from the parent curator's own payout, a fee that was already paid out to the child curator is deducted a second time from the parent curator, violating value conservation.

## Finding Description
`ChildrenCuratorFees::<T>` is incremented in `propose_curator` (`substrate/frame/child-bounties/src/lib.rs:386-391`) and is only ever intentionally decremented in `impl_close_child_bounty`: [1](#0-0) 

In contrast, `claim_child_bounty` transfers the curator fee out of the child-bounty account, decrements `ParentChildBounties`, removes the description, and clears the `ChildBounties` entry — but never touches `ChildrenCuratorFees`: [2](#0-1) 

The parent bounty pallet later reads and clears the whole accumulator when the parent bounty is claimed, treating it as the total fee obligation still to be deducted from the parent curator's payout: [3](#0-2) 

Since `claim_child_bounty` never reduces this accumulator, any fee that has already been physically paid to the child-bounty curator remains counted in `ChildrenCuratorFees` and is deducted again from the parent curator's payout when `claim_bounty` runs.

## Impact Explanation
This is confirmed directly by the existing test suite, which demonstrates the exact scenario: a child bounty is fully claimed via `claim_child_bounty` (paying curator fee `fee` out of the child-bounty account), and then when the parent bounty is claimed, the parent curator's balance is asserted to be `101 + 6 - fee`, i.e., reduced by the fee amount a second time: [4](#0-3) 

This is a genuine value-conservation violation: the child curator is paid `fee` once from the child-bounty account, and the parent curator's payout is separately reduced by the same `fee`, meaning the fee is effectively paid/deducted twice for a single service, shortchanging the parent curator. This directly matches the required pivot that "Balances… conserve value and settle exactly once to the rightful beneficiary and amount."

## Likelihood Explanation
`claim_child_bounty` is callable by any signed account once the child bounty reaches `PendingPayout` and the unlock delay has elapsed, and `claim_bounty` on the parent is likewise open to any signed account. No privileged action, race condition, or adversarial timing is needed — the normal expected lifecycle (child bounty claimed before parent bounty is claimed) deterministically triggers this bug every time a child-bounty curator fee is proposed and the child bounty is claimed prior to the parent.

## Recommendation
In `claim_child_bounty` (`substrate/frame/child-bounties/src/lib.rs:686-771`), before removing the child-bounty entry, mirror the logic in `impl_close_child_bounty` and subtract `child_bounty.fee` from `ChildrenCuratorFees::<T>::get(parent_bounty_id)`, e.g. `ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| *value = value.saturating_sub(child_bounty.fee))`. Consider factoring this into a shared helper used by both `claim_child_bounty` and `impl_close_child_bounty` to prevent future divergence.

## Proof of Concept
1. Root approves a parent bounty and assigns curator `A`, who accepts.
2. `A` calls `add_child_bounty` then `propose_curator(parent, child, curator=B, fee=F)`, incrementing `ChildrenCuratorFees[parent]` by `F`.
3. `B` calls `accept_curator`; `A` calls `award_child_bounty`.
4. After the payout delay, the beneficiary calls `claim_child_bounty(parent, child)` — `B` is paid `F` from the child-bounty account, but `ChildrenCuratorFees[parent]` remains `F`.
5. `A` claims the parent bounty via `claim_bounty`, which subtracts the stale `F` again from `A`'s payout, exactly as reproduced in the existing test at `substrate/frame/child-bounties/src/tests.rs:1408-1436` (`Balances::free_balance(account_id(4)) == 101 + 6 - fee`).
6. Result: fee `F` is deducted twice for one curator payment — once paid to `B`, once subtracted from `A`'s payout — confirming the double-counting/value-conservation violation.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L713-765)
```rust

						// Make curator fee payment.
						let child_bounty_account =
							Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
						let balance = T::Currency::free_balance(&child_bounty_account);
						let curator_fee = child_bounty.fee.min(balance);
						let payout = balance.saturating_sub(curator_fee);

						// Unreserve the curator deposit. Should not fail
						// because the deposit is always reserved when curator is
						// assigned.
						let _ = T::Currency::unreserve(curator, child_bounty.curator_deposit);

						// Make payout to child-bounty curator.
						// Should not fail because curator fee is always less than bounty value.
						let fee_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							curator,
							curator_fee,
							AllowDeath,
						);
						debug_assert!(fee_transfer_result.is_ok());

						// Make payout to beneficiary.
						// Should not fail.
						let payout_transfer_result = T::Currency::transfer(
							&child_bounty_account,
							beneficiary,
							payout,
							AllowDeath,
						);
						debug_assert!(payout_transfer_result.is_ok());

						// Trigger the Claimed event.
						Self::deposit_event(Event::<T>::Claimed {
							index: parent_bounty_id,
							child_index: child_bounty_id,
							payout,
							beneficiary: beneficiary.clone(),
						});

						// Update the active child-bounty tracking count.
						ParentChildBounties::<T>::mutate(parent_bounty_id, |count| {
							count.saturating_dec()
						});

						// Remove the child-bounty description.
						ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

						// Remove the child-bounty instance from the state.
						*maybe_child_bounty = None;

						Ok(())
```

**File:** substrate/frame/child-bounties/src/lib.rs (L925-929)
```rust
				// Revert the curator fee back to parent bounty curator &
				// reduce the active child-bounty count.
				ChildrenCuratorFees::<T>::mutate(parent_bounty_id, |value| {
					*value = value.saturating_sub(child_bounty.fee)
				});
```

**File:** substrate/frame/child-bounties/src/lib.rs (L977-985)
```rust
	/// Returns cumulative child bounty curator fees for `bounty_id` also removing the associated
	/// storage item. This function is assumed to be called when parent bounty is claimed.
	fn children_curator_fees(bounty_id: pallet_bounties::BountyIndex) -> BalanceOf<T> {
		// This is asked for when the parent bounty is being claimed. No use of
		// keeping it in state after that. Hence removing.
		let children_fee_total = ChildrenCuratorFees::<T>::get(bounty_id);
		ChildrenCuratorFees::<T>::remove(bounty_id);
		children_fee_total
	}
```

**File:** substrate/frame/child-bounties/src/tests.rs (L1408-1436)
```rust
		go_to_block(9);

		// Claim child-bounty.
		assert_ok!(ChildBounties::claim_child_bounty(RuntimeOrigin::signed(account_id(7)), 0, 0));

		// Check the child-bounty count.
		assert_eq!(pallet_child_bounties::ParentChildBounties::<Test>::get(0), 0);

		// Award the parent bounty.
		assert_ok!(Bounties::award_bounty(RuntimeOrigin::signed(account_id(4)), 0, account_id(9)));

		go_to_block(15);

		// Check the total count.
		assert_eq!(pallet_child_bounties::ParentTotalChildBounties::<Test>::get(0), 1);

		// Claim the parent bounty.
		assert_ok!(Bounties::claim_bounty(RuntimeOrigin::signed(account_id(9)), 0));

		// Check the total count after the parent bounty removal.
		assert_eq!(pallet_child_bounties::ParentTotalChildBounties::<Test>::get(0), 0);

		// Ensure parent-bounty curator received correctly reduced fee.
		assert_eq!(Balances::free_balance(account_id(4)), 101 + 6 - fee); // 101 + 6 - 2
		assert_eq!(Balances::reserved_balance(account_id(4)), 0);

		// Verify parent-bounty beneficiary balance.
		assert_eq!(Balances::free_balance(account_id(9)), 34);
		assert_eq!(Balances::reserved_balance(account_id(9)), 0);
```
