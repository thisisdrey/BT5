Audit Report

## Title
Child-bounty sub-accounts have no permissionless sweep, permanently trapping accidentally-sent balance/assets after closure/claim - (File: substrate/frame/child-bounties/src/lib.rs)

## Summary
`pallet-child-bounties` derives a deterministic sub-account per child bounty via `child_bounty_account_id(parent_bounty_id, child_bounty_id)` [1](#0-0)  that receives and pays out native balance in `claim_child_bounty` and `impl_close_child_bounty`, after which the corresponding `ChildBounties` storage entry is permanently removed [2](#0-1) . Unlike `pallet-bounties`, which was given a permissionless `reclaim_bounty_funds` extrinsic to sweep stranded funds/assets from closed bounty accounts back to the treasury [3](#0-2) , `pallet-child-bounties` has no analogous extrinsic, and `reclaim_bounty_funds` only targets `Self::bounty_account_id(bounty_id)` (the parent account), never the child sub-account.

## Finding Description
`impl_close_child_bounty` transfers the child-bounty account's native `free_balance` to the parent bounty account and then unconditionally sets `*maybe_child_bounty = None`, deleting the storage entry [4](#0-3) . Likewise, `claim_child_bounty`'s inner closure pays curator fee and payout computed from the account's balance at that moment, then removes the entry (as referenced in the claim, lines 714-763 of the same file). In both cases only `T::Currency::free_balance`/`transfer` is used — there is no equivalent of `TransferAllAssets::force_transfer_all_assets` used in `pallet-bounties::close_bounty`/`reclaim_bounty_funds` [5](#0-4) .

Because `child_bounty_account_id` is a pure deterministic function of `(parent_bounty_id, child_bounty_id)`, the address is publicly computable even after the `ChildBounties` entry is removed. Any subsequent transfer to that address (accidental transfer, dust, asset airdrop, or a delayed/duplicate transfer racing the closure) becomes permanently unreachable, since:
- `pallet-child-bounties` has no dispatchable that references a child-bounty sub-account except during an active child bounty's lifecycle (confirmed via full scan of the pallet's call list — no `reclaim_child_bounty_funds` or similar exists).
- `pallet-bounties::reclaim_bounty_funds` only operates on `Self::bounty_account_id(bounty_id)`, the parent account, not on any child sub-account [6](#0-5) .

This is the identical stuck-funds pattern that was fixed for `pallet-bounties` in the PR referenced by `prdoc/pr_11045.prdoc` (fixing paritytech/polkadot-sdk#10996), but that fix was never propagated to the sibling pallet sharing the same sub-account architecture.

## Impact Explanation
This satisfies the "permanent user-fund or bridge-state lock" category in the impact gate: any native balance (or asset, if configured) sent to a closed/claimed child-bounty sub-account is irrecoverable by any extrinsic in the codebase. Because sub-account addresses are deterministically derivable off-chain, this is a genuine, repeatable fund-loss vector distinct from ordinary user error, since no rescue path exists anywhere in the pallet or its sibling `pallet-bounties`.

## Likelihood Explanation
Trigger requires only an ordinary, unprivileged account transferring value to a publicly-computable address after (or racing) a normal, permissionless child-bounty lifecycle completion (`claim_child_bounty` / `close_child_bounty`). No attacker privilege, governance action, or compromised infrastructure is needed — matching the exact scenario validated for the sibling `pallet-bounties` fix's regression test `reclaim_bounty_funds_works_after_accidental_refund` [7](#0-6) , which is absent for child bounties.

## Recommendation
Add a permissionless `reclaim_child_bounty_funds(parent_bounty_id, child_bounty_id)` extrinsic to `pallet-child-bounties`, gated on `!ChildBounties::<T>::contains_key(parent_bounty_id, child_bounty_id)`, that sweeps all native balance (and any configured assets via a `TransferAllAssets`-style mechanism) from `child_bounty_account_id(parent_bounty_id, child_bounty_id)` back to the parent bounty account, with pay-on-noop semantics to prevent griefing, mirroring `pallet-bounties::reclaim_bounty_funds`.

## Proof of Concept
1. Create a parent bounty, propose/accept curator, `add_child_bounty` to create child bounty `(0, 0)`; compute `A = ChildBounties::child_bounty_account_id(0, 0)`.
2. Complete lifecycle: `award_child_bounty` → `claim_child_bounty(0, 0)`, which pays out and removes the `ChildBounties` entry per [8](#0-7) .
3. Any third party sends a plain `Balances::transfer` to `A`.
4. `Balances::free_balance(A)` is now non-zero, but no entry exists in `ChildBounties` for `(0,0)` and no dispatchable in either `pallet-child-bounties` or `pallet-bounties::reclaim_bounty_funds` (which only targets `bounty_account_id(0)`, the parent account) can sweep it — funds are permanently stranded.

### Citations

**File:** substrate/frame/child-bounties/src/lib.rs (L934-951)
```rust
				// Transfer fund from child-bounty to parent bounty.
				let parent_bounty_account =
					pallet_bounties::Pallet::<T>::bounty_account_id(parent_bounty_id);
				let child_bounty_account =
					Self::child_bounty_account_id(parent_bounty_id, child_bounty_id);
				let balance = T::Currency::free_balance(&child_bounty_account);
				let transfer_result = T::Currency::transfer(
					&child_bounty_account,
					&parent_bounty_account,
					balance,
					AllowDeath,
				); // Should not fail; child bounty account gets this balance during creation.
				debug_assert!(transfer_result.is_ok());

				// Remove the child-bounty description.
				ChildBountyDescriptionsV1::<T>::remove(parent_bounty_id, child_bounty_id);

				*maybe_child_bounty = None;
```

**File:** substrate/frame/bounties/src/lib.rs (L917-924)
```rust
					let bounty_account = Self::bounty_account_id(bounty_id);

					BountyDescriptions::<T, I>::remove(bounty_id);

					T::TransferAllAssets::force_transfer_all_assets(
						&bounty_account,
						&Self::account_id(),
					)?;
```

**File:** substrate/frame/bounties/src/lib.rs (L1058-1090)
```rust
		#[pallet::call_index(11)]
		#[pallet::weight(<T as Config<I>>::WeightInfo::reclaim_bounty_funds())]
		pub fn reclaim_bounty_funds(
			origin: OriginFor<T>,
			#[pallet::compact] bounty_id: BountyIndex,
		) -> DispatchResultWithPostInfo {
			ensure_signed(origin)?;

			// A live bounty still manages its account, so leave it untouched.
			ensure!(!Bounties::<T, I>::contains_key(bounty_id), Error::<T, I>::BountyStillActive);

			debug_assert!(
				T::ChildBountyManager::child_bounties_count(bounty_id) == 0,
				"child bounties should not exist for a closed bounty"
			);

			let bounty_account = Self::bounty_account_id(bounty_id);
			let treasury_account = Self::account_id();

			let transferred = T::TransferAllAssets::force_transfer_all_assets(
				&bounty_account,
				&treasury_account,
			)?;

			// Free only if something moved, otherwise paid to prevent griefing.
			if !transferred {
				return Ok(Pays::Yes.into());
			}

			Self::deposit_event(Event::<T, I>::BountyFundsReclaimed { bounty_id });

			Ok(Pays::No.into())
		}
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
