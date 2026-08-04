## Finding: Silently‑ignored transfer failures permanently strand child‑bounty funds

The external report's core defect is that a token transfer's success/failure is never checked, so a failed transfer is treated as if it succeeded and the caller (liquidator) is deprived of funds. The direct structural analog in this repository is in `pallet-child-bounties`, where `T::Currency::transfer` results for both the curator fee and the beneficiary payout are checked only with `debug_assert!`, which compiles to a no-op in production (`--release`) builds.

### Title
Ignored transfer failures in `claim_child_bounty` permanently lock funds in the child-bounty sub-account - (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
`Pallet::claim_child_bounty` moves the curator fee and beneficiary payout out of the per-child-bounty sub-account using `T::Currency::transfer(..., AllowDeath)`, but only asserts success via `debug_assert!`, which is stripped out in non-debug builds. [1](#0-0)  If either transfer actually fails (e.g. because the destination account does not exist and the transferred amount is below the chain's Existential Deposit), the call still proceeds to remove the child-bounty record and emit `Claimed`, permanently erasing all bookkeeping for those funds while they remain stuck in the child-bounty account.

### Finding Description
`claim_child_bounty` computes `curator_fee` and `payout` from the child-bounty account's balance and unconditionally transfers them with `AllowDeath`: [2](#0-1) 

Both transfer results (`fee_transfer_result`, `payout_transfer_result`) are only checked with `debug_assert!`, a macro that is compiled out entirely in release builds. Immediately afterward, regardless of whether the transfers actually succeeded, the code unconditionally:
- fires `Event::Claimed`,
- decrements `ParentChildBounties`,
- removes `ChildBountyDescriptionsV1`,
- sets `*maybe_child_bounty = None` (deleting the only record that referenced these funds). [3](#0-2) 

Unlike the parent `pallet-bounties`, which was later given a `reclaim_bounty_funds` extrinsic to dust/recover balance stranded in a bounty account after such issues (see the fix tracked in `substrate/frame/bounties/src/lib.rs` and `pr_11045.prdoc`), `pallet-child-bounties` has no equivalent recovery mechanism. [4](#0-3) 

A concrete failure mode: `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` fails when `beneficiary` has zero prior balance and `payout` is below the runtime's `ExistentialDeposit`. This is easily reachable because `payout` is simply `balance.saturating_sub(curator_fee)` with no minimum-amount check, and the beneficiary is an attacker-chosen (or already-dead) account supplied earlier via `award_child_bounty`.

### Impact Explanation
When the transfer silently fails, the funds are permanently locked in the child-bounty sub-account: the `ChildBounties` storage entry that would let it be re-processed is deleted, and there is no `reclaim`/`recover` call in this pallet to sweep the account, unlike its sibling pallet. This matches the "permanent user-fund lock" impact category directly: value is neither delivered to the intended beneficiary/curator nor recoverable through any documented path.

### Likelihood Explanation
Reachable by any unprivileged participant already in the normal bounty workflow (parent curator assigns a child-bounty curator, curator/anyone awards to an attacker-controlled or zero-balance beneficiary with a small residual payout, then anyone calls `claim_child_bounty`). No malicious peer, validator, relayer, or admin/governance action is required — it is triggerable purely through public dispatchables under ordinary parameter choices (small child-bounty value/fee combinations relative to `ExistentialDeposit`).

### Recommendation
Replace the `debug_assert!` checks on `fee_transfer_result` and `payout_transfer_result` with proper `?`/`ensure!`-based error propagation so a failed transfer aborts the call before state (the child-bounty record) is destroyed. Additionally, add a `reclaim_child_bounty_funds`-style recovery extrinsic analogous to the one added to `pallet-bounties`, so any dust/failed-transfer balance left in a child-bounty account after the record is gone can still be swept back to the parent bounty or treasury.

### Proof of Concept
1. Create a parent bounty and a child bounty whose `value` and curator `fee` are chosen such that `payout = value - fee` is smaller than the runtime's `ExistentialDeposit`.
2. Award the child bounty to a `beneficiary` account that currently holds zero balance (so it does not exist in `System::Account`).
3. After the payout delay, call `claim_child_bounty`. In a release build, `T::Currency::transfer(&child_bounty_account, beneficiary, payout, AllowDeath)` returns `Err` (below existential deposit for a non-existent destination), but `debug_assert!` is compiled out, so execution continues.
4. Observe: `Event::Claimed` is emitted with the stated `payout`, `ChildBounties` entry is removed, `ParentChildBounties` count decrements — yet `Balances::free_balance(child_bounty_account)` still holds the un-transferred `payout` amount, and no dispatchable exists in `pallet-child-bounties` to recover it.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L714-744)
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
