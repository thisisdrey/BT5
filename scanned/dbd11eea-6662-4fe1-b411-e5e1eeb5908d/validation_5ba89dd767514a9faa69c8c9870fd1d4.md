### Title
Stranded funds in closed child-bounty accounts have no reclaim path — (File: `substrate/frame/child-bounties/src/lib.rs`)

### Summary
The reported GMX Trove bug is a case of an intermediary/escrow sub-account that receives funds during normal operation but has no reachable "sweep" path once the owning object transitions to a terminal/closed state — the only claim function is gated on a status that closing makes permanently unreachable. `pallet-bounties` in this repository had the same broken invariant: a bounty's derived sub-account (`bounty_account_id`) could receive assets (accidental transfers, asset dust) that became permanently stranded once the bounty was closed and removed from `Bounties` storage, because no permissionless function could sweep a *closed* bounty's account. This was fixed by adding `reclaim_bounty_funds` (see `prdoc/pr_11045.prdoc` and `substrate/frame/bounties/src/lib.rs`), which permissionlessly transfers all remaining native/asset balance in `Self::bounty_account_id(bounty_id)` back to treasury once the bounty no longer exists in `Bounties::<T, I>`.

`pallet-child-bounties`, which is architecturally the same pattern (a `child_bounty_account_id` sub-account that holds funds transferred out of the parent bounty account for the duration of the child bounty's lifecycle) does not have an equivalent reclaim/sweep extrinsic. Grepping `substrate/frame/child-bounties/src/lib.rs` confirms there is no `reclaim`, `force_transfer_all_assets`, or similar permissionless sweep for a child bounty's derived account, even though the pallet exposes `close_child_bounty`, `award_child_bounty`/`claim_child_bounty`, and `unassign_curator` state-machine transitions analogous to the parent pallet's bounty lifecycle that was deemed to need this fix. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
`child_bounty_account_id` is a per-child-bounty derived account that holds the child bounty's value (and any residual dust/asset transfers sent to it, intentionally or accidentally, exactly as could happen to the parent bounty account per the `reclaim_bounty_funds_works_after_accidental_refund` test). Once a child bounty is closed via `close_child_bounty` or fully claimed via `claim_child_bounty`/`award_child_bounty`, its storage entry (`ChildBounties`) is removed, and — mirroring the exact pre-fix state of `pallet-bounties` — there is no dispatchable in `pallet-child-bounties` that can move any leftover balance sitting in that now-orphaned sub-account anywhere. The only mechanism the sibling pallet has for this class of stranded-fund bug is `reclaim_bounty_funds`, gated on `!Bounties::<T, I>::contains_key(bounty_id)`; child bounties have no analogous gate/function keyed on `!ChildBounties::<T>::contains_key(...)`. [4](#0-3) [5](#0-4) 

This is a direct architectural analog to the report's core broken invariant: an intermediary escrow account (trove / bounty sub-account) accumulates value during the object's active lifetime, but the *only* mechanisms to move funds out of it are lifecycle-state-gated calls, and the terminal ("closed"/"destroyed") state removes the storage key those calls depend on, permanently orphaning any balance left in the sub-account.

### Impact Explanation
Funds sent to a closed child bounty's derived account (through griefing transfers, delayed asset settlement, dust from partial payments, or operator error) become permanently locked with no path to recovery by anyone — not the treasury, not governance, not the original curator/beneficiary — since the account is never referenced again once `ChildBounties` no longer contains the id. This is exactly the "permanent user-fund or bridge-state lock" class explicitly called out as in-scope.

### Likelihood Explanation
No privileged actor, malicious relayer, or governance action is needed: anyone can transfer tokens/assets to a knowable, deterministically-derived (`AccountIdConversion`) child-bounty sub-account after it has closed, exactly as demonstrated for the parent pallet in `reclaim_bounty_funds_works_after_accidental_refund`. The precondition (closed child bounty with residual balance) is trivially reachable through the pallet's normal `close_child_bounty`/`claim_child_bounty` flows plus an ordinary transfer.

### Recommendation
Add a permissionless `reclaim_child_bounty_funds`-style extrinsic to `pallet-child-bounties`, gated on `!ChildBounties::<T>::contains_key(parent_bounty_id, child_bounty_id)`, that transfers any residual native/asset balance from `Self::child_bounty_account_id(...)` to the parent bounty account (or treasury), mirroring the fix already applied to `pallet-bounties::reclaim_bounty_funds`.

### Proof of Concept
1. Create a parent bounty and a child bounty; fund it normally through the standard lifecycle.
2. Complete the child bounty lifecycle to closure (`claim_child_bounty` or `close_child_bounty`), which removes the `ChildBounties` entry for that id.
3. Transfer an arbitrary amount of the native currency (or a configured asset) directly to `ChildBounties::child_bounty_account_id(parent_bounty_id, child_bounty_id)`.
4. Observe that no extant call in `pallet-child-bounties` references this account any longer — there is no `reclaim_bounty_funds` analog — so the balance is permanently stranded, exactly reproducing the `reclaim_bounty_funds_works_after_accidental_refund` scenario from `pallet-bounties` but without the pallet-bounties fix being present for child bounties. [5](#0-4)

### Citations

**File:** prdoc/pr_11045.prdoc (L1-14)
```text
title: '[pallet-bounties]: add `reclaim_bounty_funds` to reclaim stranded funds from
  closed bounty accounts'
doc:
- audience: Runtime Dev
  description: |-
    fixes https://github.com/paritytech/polkadot-sdk/issues/10996

    This PR adds a permissionless `reclaim_bounty_funds` extrinsic that moves all
    funds stranded in a closed bounty's account back to the treasury in a single
    call. It reclaims both the native token and any fungible assets configured via
    the `TransferAllAssets` associated type. Native funds are moved using
    `transfer_all` semantics (reducible balance with `Expendable` preservation) so
    locks and freezes are respected. The call is free on success and paid on a no-op,
    so it cannot be used to grief the network.
```

**File:** substrate/frame/bounties/src/lib.rs (L1048-1090)
```rust
		///
		/// Permissionless. Moves all remaining assets from a closed bounty's account back to the
		/// treasury in a single call. Which assets are swept depends on the `TransferAllAssets`
		/// configuration.
		///
		/// The call is free if funds were reclaimed and paid otherwise, so no-op calls cannot be
		/// used to grief the network. Emits `BountyFundsReclaimed` on success.
		///
		/// ## Complexity
		/// - O(A) where A is the number of relevant assets configured in `TransferAllAssets`.
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

**File:** substrate/frame/child-bounties/src/lib.rs (L18-45)
```rust
//! # Child Bounties Pallet ( `pallet-child-bounties` )
//!
//! ## Child Bounty
//!
//! > NOTE: This pallet is tightly coupled with `pallet-treasury` and `pallet-bounties`.
//!
//! With child bounties, a large bounty proposal can be divided into smaller chunks,
//! for parallel execution, and for efficient governance and tracking of spent funds.
//! A child bounty is a smaller piece of work, extracted from a parent bounty.
//! A curator is assigned after the child bounty is created by the parent bounty curator,
//! to be delegated with the responsibility of assigning a payout address once the specified
//! set of tasks is completed.
//!
//! ## Interface
//!
//! ### Dispatchable Functions
//!
//! Child Bounty protocol:
//! - `add_child_bounty` - Add a child bounty for a parent bounty to for dividing the work in
//!   smaller tasks.
//! - `propose_curator` - Assign an account to a child bounty as candidate curator.
//! - `accept_curator` - Accept a child bounty assignment from the parent bounty curator, setting a
//!   curator deposit.
//! - `award_child_bounty` - Close and pay out the specified amount for the completed work.
//! - `claim_child_bounty` - Claim a specific child bounty amount from the payout address.
//! - `unassign_curator` - Unassign an accepted curator from a specific child bounty.
//! - `close_child_bounty` - Cancel the child bounty for a specific treasury amount and close the
//!   bounty.
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
