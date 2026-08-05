Audit Report

## Title
Ignored transfer failures in `claim_child_bounty` permanently lock funds in the child-bounty sub-account - (File: `substrate/frame/child-bounties/src/lib.rs`)

## Summary
`Pallet::claim_child_bounty` computes `curator_fee` and `payout` from the child-bounty sub-account balance and transfers them via `T::Currency::transfer(..., AllowDeath)`, but only validates the results with `debug_assert!`, which is a no-op in release builds. If either transfer fails, the pallet still deletes the `ChildBounties` storage entry, decrements `ParentChildBounties`, removes the description, and emits `Claimed`, permanently orphaning the un-transferred funds in the child-bounty account with no path to recovery.

## Finding Description
In `claim_child_bounty`, the fee and payout transfers are dispatched and their `Result`s are discarded except for a `debug_assert!` check: [1](#0-0) . Because `debug_assert!` compiles to nothing in `--release` builds (which is how production chains are built), a failed transfer does not abort execution or return an error. The function then unconditionally proceeds to fire `Event::Claimed`, decrement `ParentChildBounties`, remove `ChildBountyDescriptionsV1`, and set `*maybe_child_bounty = None` [2](#0-1) , destroying the only bookkeeping record that referenced the stranded balance.

A concrete failure mode: the beneficiary-payout transfer uses `AllowDeath` and will fail if the destination account does not exist and the transferred `payout` amount is below the runtime's Existential Deposit. Since `payout = balance.saturating_sub(curator_fee)` has no minimum-amount enforcement, and `beneficiary` is supplied earlier via `award_child_bounty` without existence checks, an attacker (or an ordinary participant with unlucky parameter choices) can trigger this deterministically.

Unlike the sibling pallet `pallet-bounties`, which added `reclaim_bounty_funds` to sweep balance stranded in a bounty account after the bounty record is gone [3](#0-2) , `pallet-child-bounties` has no equivalent recovery extrinsic — confirmed by the absence of any `reclaim`/`BountyFundsReclaimed`-style call in `substrate/frame/child-bounties/src/lib.rs`.

## Impact Explanation
This matches the "permanent user-fund lock" impact category in the Polkadot SDK Impact Gate: funds intended for the curator fee and/or beneficiary payout become permanently stuck in the per-child-bounty sub-account once the corresponding `ChildBounties` record is deleted, with no dispatchable in this pallet capable of recovering them. The exact corrupted state is the `ChildBounties<T>` entry for `(parent_bounty_id, child_bounty_id)` being erased while `Balances::free_balance(child_bounty_account)` still holds the undelivered `payout`/`curator_fee` amount.

## Likelihood Explanation
The bug is reachable entirely through public dispatchables in the normal bounty workflow: a parent curator assigns a child-bounty curator, the curator (or via other public paths) awards the child bounty to a beneficiary account with zero prior balance, and once the payout delay elapses, any signed account can call `claim_child_bounty`. No privileged, governance, validator, or off-chain/relayer capability is required — only ordinary parameter selection (small child-bounty value/fee relative to `ExistentialDeposit`) is needed, making this reliably reproducible.

## Recommendation
Replace the `debug_assert!` checks on `fee_transfer_result` and `payout_transfer_result` with `?`-based error propagation so that a failed transfer aborts the call via `try_mutate_exists` before any state (the child-bounty record, `ParentChildBounties` count, description) is destroyed. Additionally, introduce a `reclaim_child_bounty_funds`-style extrinsic analogous to `pallet-bounties::reclaim_bounty_funds` to sweep any residual balance left in a child-bounty account whose record has already been removed.

## Proof of Concept
1. Create a parent bounty and a child bounty with `value` and curator `fee` chosen so `payout = value - fee` is below the runtime's `ExistentialDeposit`.
2. Award the child bounty to a `beneficiary` account with zero balance (non-existent in `System::Account`).
3. After the payout delay, call `claim_child_bounty` in a release build: `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` returns `Err`, but `debug_assert!(payout_transfer_result.is_ok())` at [4](#0-3)  is compiled out, so execution proceeds unchecked.
4. Observe `Event::Claimed` emitted with the stated `payout`, `ChildBounties` entry removed, and `ParentChildBounties` decremented, while `Balances::free_balance(child_bounty_account)` still holds the un-transferred `payout`, with no recovery dispatchable available in `pallet-child-bounties`.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L726-744)
```rust
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
```

**File:** substrate/frame/child-bounties/src/lib.rs (L746-765)
```rust
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

**File:** substrate/frame/bounties/src/tests.rs (L2058-2096)
```rust
#[test]
fn reclaim_bounty_funds_works_after_accidental_refund() {
	ExtBuilder::default().build_and_execute(|| {
		Balances::make_free_balance_be(&Treasury::account_id(), 101);

		// Full lifecycle: propose → approve → fund → curator → award → claim
		assert_ok!(Bounties::propose_bounty(RuntimeOrigin::signed(0), 50, b"12345".to_vec()));
		assert_ok!(Bounties::approve_bounty(RuntimeOrigin::root(), 0));
		go_to_block(2);

		let fee = 4;
		Balances::make_free_balance_be(&4, 10);
		assert_ok!(Bounties::propose_curator(RuntimeOrigin::root(), 0, 4, fee));
		assert_ok!(Bounties::accept_curator(RuntimeOrigin::signed(4), 0));
		assert_ok!(Bounties::award_bounty(RuntimeOrigin::signed(4), 0, 3));
		go_to_block(5);
		assert_ok!(Bounties::claim_bounty(RuntimeOrigin::signed(1), 0));

		// Bounty is now fully closed; verify it is gone from storage.
		assert!(pallet_bounties::Bounties::<Test>::get(0).is_none());

		let bounty_account = Bounties::bounty_account_id(0);
		// Account should already be empty after claim.
		assert_eq!(Balances::free_balance(&bounty_account), 0);

		// Simulate someone accidentally sending funds to the closed bounty account.
		Balances::make_free_balance_be(&bounty_account, 25);
		assert_eq!(Balances::free_balance(&bounty_account), 25);

		let treasury_before = Treasury::pot();

		// Dust the account.
		assert_ok!(Bounties::reclaim_bounty_funds(RuntimeOrigin::signed(99), 0));
		assert_eq!(last_event(), BountiesEvent::BountyFundsReclaimed { bounty_id: 0 },);

		assert_eq!(Balances::free_balance(&bounty_account), 0);
		assert!(Treasury::pot() > treasury_before);
	});
}
```
