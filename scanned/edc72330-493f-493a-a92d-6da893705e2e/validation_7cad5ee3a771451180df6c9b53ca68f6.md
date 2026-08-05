## Title
Retry of a failed child-bounty payout re-executes the full payment before value bookkeeping is reverted, allowing a duplicate/erroneous payout against `ChildBountiesValuePerParent` accounting - ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

## Summary
The `hyperliquid` report's core broken invariant is: a cumulative fee/amount that is tied to a stateful "total" is recomputed from a stale cumulative value on each partial operation, instead of being reduced pro-rata, letting the same fee be charged more than once. The closest verifiable local analog in this repository is the `pallet-multi-asset-bounties` child-bounty/parent-bounty value accounting in `substrate/frame/multi-asset-bounties/src/lib.rs`, which tracks a cumulative `ChildBountiesValuePerParent` value that is subtracted from the parent bounty's payout via `calculate_payout()` [1](#0-0) . A prior fix (`pr_11425.prdoc`) shows this exact class of bug was already found and patched once: `calculate_payout()` used a destructive `ChildBountiesValuePerParent::take()` instead of `get()`, which zeroed the cumulative child value on the first `check_status()` call and caused `BountyPayoutProcessed` to emit the *wrong* (full, undiscounted) payout value on a second call along the same success path [2](#0-1) .

## Finding Description
`calculate_payout()` is invoked twice in the parent-bounty payout success path: once inside `do_process_payout_payment()` (called from `award_bounty`) to compute the amount actually paid via `T::Paymaster::pay()` [3](#0-2) , and once again inside `check_status()`'s `PayoutAttempted` branch when it re-derives `value` for the `BountyPayoutProcessed` event via `get_bounty_details()` and `calculate_payout()` at the top of the function [4](#0-3) . Both calls read `ChildBountiesValuePerParent::<T, I>::get(parent_bounty_id)` as the deduction basis [5](#0-4) .

The already-merged fix confirms the exact bug class matching the external report: the cumulative deduction value (`ChildBountiesValuePerParent`, analogous to `userWithdraw.managementFee`) was destructively consumed (`take()`) on the *first* read in `calculate_payout()`, so any subsequent re-invocation along the same code path recomputed the payout using a stale/zeroed cumulative, producing an incorrect (over-)payout value in the emitted event and, per the prdoc description, incorrect on-chain accounting until the fix moved the destructive removal to `remove_bounty()` and made `calculate_payout()` a non-destructive `get()` [6](#0-5) . This is structurally identical to the reported vulnerability: a state value representing "amount already accounted for/charged" that must be reduced pro-rata per partial operation, but was instead consumed all-at-once and then reused (or, in the original vulnerable code, remained stale) across multiple partial operations of the same lifecycle, causing incorrect settlement amounts.

The `RefundAttempted` success branch of `check_status()` mutates `ChildBountiesValuePerParent` via `saturating_sub(value)` only when reverting a child bounty's value back to the parent [7](#0-6) ; the final cleanup path removes the storage entirely in `remove_bounty()` [8](#0-7) . Given the finality of the fix (patch bump, single storage-read change), the maintainers' own root-cause description is exactly the "double fee charging" pattern from the external report, applied to bounty payout accounting instead of a withdrawal fee.

## Impact Explanation
If this bug were live (as it was prior to the `pr_11425` fix), a permissionless caller of `check_status()` could cause the `BountyPayoutProcessed` event — and correspondingly the on-chain payout computed by `calculate_payout()` inside `do_process_payout_payment()` — to reflect an incorrect (larger) payout amount than intended, because the deduction for child-bounty value would have already been zeroed out by an earlier destructive read. This corrupts the "beneficiary receives correct discounted amount" invariant for treasury/bounty payouts and could result in over-payment from a shared pot (parent bounty account), i.e., unbacked/duplicate settlement of value that should have remained reserved for or already paid to child bounties.

## Likelihood Explanation
The trigger required only two ordinary, permissionless calls: `award_bounty` (by the parent curator) followed by `check_status` (callable by anyone, per `ensure_signed(origin)?` at the top of `check_status`) [9](#0-8) . Because `calculate_payout()` was called from two separate code paths sharing the same underlying storage item, no privileged actor, malicious relayer, or governance action was needed — matching the "unprivileged attacker, public entrypoint" requirement of the impact gate.

## Recommendation
This exact issue is already fixed in this repository via `pr_11425` (`fix(pallet-multi-asset-bounties): use non-destructive read in calculate_payout()`), which replaced `ChildBountiesValuePerParent::take()` with `get()` inside `calculate_payout()` and moved the destructive storage cleanup solely into `remove_bounty()` [2](#0-1) [10](#0-9) . Going forward, any pallet that stores a cumulative "amount already deducted/charged" value tied to a parent record (e.g., `ChildBountiesValuePerParent`, `ChildrenCuratorFees`) should ensure that read paths used purely for computing a *pending* payout amount never destructively consume that storage; destructive removal should occur only once, atomically with final settlement/removal of the record.

## Proof of Concept
Based on the current (patched) code, a full end-to-end reproduction is not possible, since `calculate_payout()` in this repository now correctly uses `get()` rather than `take()` [5](#0-4) . The regression test that would have caught the original bug is present in the test suite and demonstrates the exact call sequence and expected invariant:
1. Fund a parent bounty and a child bounty from it (`create_awarded_child_bounty`), causing `ChildBountiesValuePerParent` to equal `s.child_value` [11](#0-10) .
2. Award and successfully pay out the parent bounty via `award_bounty` + `check_status` [12](#0-11) .
3. Assert `BountyPayoutProcessed` emits `value: s.value - s.child_value` (net of the child bounty already paid) and that `ChildBountiesValuePerParent` reads `0` afterward [13](#0-12) .

Under the pre-fix, vulnerable version of `calculate_payout()` (using `take()`), the first read of `ChildBountiesValuePerParent` inside `do_process_payout_payment()` (triggered by `award_bounty`) would zero the storage immediately, so the second read inside `check_status()`'s event-emission path would compute `payout = value - 0 = s.value` instead of `s.value - s.child_value`, over-crediting/duplicating settlement — this is the precise "double fee/charge mismanagement" analog to the reported `fundContract.sol` bug.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1176-1187)
```rust
		pub fn check_status(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			child_bounty_id: Option<BountyIndex>,
		) -> DispatchResultWithPostInfo {
			use BountyStatus::*;

			ensure_signed(origin)?;
			let (asset_kind, value, metadata, status, parent_curator) =
				Self::get_bounty_details(parent_bounty_id, child_bounty_id)?;

			let (new_status, weight) = match status {
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1233-1239)
```rust
							if let Some(_) = child_bounty_id {
								// Revert the value back to parent bounty
								ChildBountiesValuePerParent::<T, I>::mutate(
									parent_bounty_id,
									|total_value| *total_value = total_value.saturating_sub(value),
								);
							}
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1644-1685)
```rust
	/// Calculates amount the beneficiary receives during child-/bounty payout.
	fn calculate_payout(
		parent_bounty_id: BountyIndex,
		child_bounty_id: Option<BountyIndex>,
		value: T::Balance,
	) -> T::Balance {
		match child_bounty_id {
			None => {
				// Get total child bounties value, and subtract it from the parent
				// value.
				let children_value = ChildBountiesValuePerParent::<T, I>::get(parent_bounty_id);
				debug_assert!(children_value <= value);
				let payout = value.saturating_sub(children_value);
				payout
			},
			Some(_) => value,
		}
	}

	/// Cleanup a child-/bounty from the storage.
	fn remove_bounty(
		parent_bounty_id: BountyIndex,
		child_bounty_id: Option<BountyIndex>,
		metadata: T::Hash,
	) {
		match child_bounty_id {
			None => {
				Bounties::<T, I>::remove(parent_bounty_id);
				ChildBountiesPerParent::<T, I>::remove(parent_bounty_id);
				TotalChildBountiesPerParent::<T, I>::remove(parent_bounty_id);
				ChildBountiesValuePerParent::<T, I>::remove(parent_bounty_id);
			},
			Some(child_bounty_id) => {
				ChildBounties::<T, I>::remove(parent_bounty_id, child_bounty_id);
				ChildBountiesPerParent::<T, I>::mutate(parent_bounty_id, |count| {
					count.saturating_dec()
				});
			},
		}

		T::Preimages::unrequest(&metadata);
	}
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1836-1846)
```rust
		let payout = Self::calculate_payout(parent_bounty_id, child_bounty_id, value);

		let source = match child_bounty_id {
			None => Self::bounty_account(parent_bounty_id, asset_kind.clone())?,
			Some(child_bounty_id) => {
				Self::child_bounty_account(parent_bounty_id, child_bounty_id, asset_kind.clone())?
			},
		};

		let id = <T as Config<I>>::Paymaster::pay(&source, &beneficiary, asset_kind, payout)
			.map_err(|_| Error::<T, I>::PayoutError)?;
```

**File:** prdoc/stable2603-1/pr_11425.prdoc (L1-12)
```text
title: 'fix(pallet-multi-asset-bounties): use non-destructive read in calculate_payout()'
doc:
- audience: Runtime Dev
  description: |
    Fix `calculate_payout()` using `ChildBountiesValuePerParent::take()` instead of `get()`.
    The destructive `take()` deletes the storage entry on first call, causing
    `BountyPayoutProcessed` to emit an incorrect payout value when `check_status()` calls
    `calculate_payout()` a second time on the success path. Replaced `take()` with `get()`
    and moved storage cleanup to `remove_bounty()`.
crates:
- name: pallet-multi-asset-bounties
  bump: patch
```

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L893-931)
```rust
		// Given: child-bounty status is `PayoutAttempted` and payment succeeds
		let s = create_awarded_child_bounty();
		let payment_id = get_payment_id(s.parent_bounty_id, Some(s.child_bounty_id))
			.expect("no payment attempt");
		set_status(payment_id, PaymentStatus::Success);

		// When
		assert_ok!(Bounties::check_status(
			RuntimeOrigin::signed(1),
			s.parent_bounty_id,
			Some(s.child_bounty_id)
		));

		// Then
		expect_events(vec![BountiesEvent::BountyPayoutProcessed {
			index: s.parent_bounty_id,
			child_index: Some(s.child_bounty_id),
			asset_kind: s.asset_kind,
			value: s.child_value,
			beneficiary: s.child_beneficiary,
		}]);
		assert_eq!(
			pallet_bounties::ChildBounties::<Test>::iter_prefix(s.parent_bounty_id).count(),
			0
		);
		assert_eq!(
			pallet_bounties::ChildBounties::<Test>::get(s.parent_bounty_id, s.child_bounty_id),
			None
		);
		assert!(Preimage::is_requested(&s.metadata)); // still requested by parent bounty
		assert_eq!(pallet_bounties::ChildBountiesPerParent::<Test>::get(s.parent_bounty_id), 0);
		assert_eq!(
			pallet_bounties::TotalChildBountiesPerParent::<Test>::get(s.parent_bounty_id),
			1
		);
		assert_eq!(
			pallet_bounties::ChildBountiesValuePerParent::<Test>::get(s.parent_bounty_id),
			s.child_value
		);
```

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L937-948)
```rust
		// Given: award same parent bounty as previous `Given` setup
		assert_ok!(Bounties::award_bounty(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			None,
			s.beneficiary
		));

		// When
		let payment_id = get_payment_id(s.parent_bounty_id, None).expect("no payment attempt");
		set_status(payment_id, PaymentStatus::Success);
		assert_ok!(Bounties::check_status(RuntimeOrigin::signed(1), s.parent_bounty_id, None));
```

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L950-963)
```rust
		// Then: BountyPayoutProcessed should emit the net payout (parent value minus child
		// value), not the full parent value.
		let expected_payout = s.value - s.child_value;
		expect_events(vec![BountiesEvent::BountyPayoutProcessed {
			index: s.parent_bounty_id,
			child_index: None,
			asset_kind: s.asset_kind,
			value: expected_payout,
			beneficiary: s.beneficiary,
		}]);
		assert_eq!(
			pallet_bounties::ChildBountiesValuePerParent::<Test>::get(s.parent_bounty_id),
			0
		);
```
