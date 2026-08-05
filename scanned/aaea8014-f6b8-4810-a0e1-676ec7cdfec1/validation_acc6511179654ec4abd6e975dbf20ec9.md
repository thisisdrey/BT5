## Title
`award_bounty` computes parent-bounty payout from stored `value` minus `ChildBountiesValuePerParent`, not from the bounty account's real asset balance, allowing a permanent payout revert/lock after `increase_value` or after asset-level slippage - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

## Summary
`pallet-multi-asset-bounties` mirrors the `USDMPegRecovery` bug class exactly: user/curator-visible accounting (`Bounty.value`, `ChildBountiesValuePerParent`) is tracked purely in on-chain storage and is used to compute payout amounts, while the actual spendable balance of the bounty's derived sub-account is whatever the configured `AssetKind`/`Paymaster` actually delivered there. There is no invariant enforcing that `calculate_payout()`'s result is bounded by, or reconciled against, the bounty account's real balance before a payment is attempted.

## Finding Description
`Bounty::value` is a value chosen by governance/curator action, not a live read of asset reserves: [1](#0-0) 

`award_bounty` reads `value` straight from storage via `get_bounty_details` and passes it to `do_process_payout_payment`, with no comparison against the bounty account's actual balance: [2](#0-1) 

For a parent bounty, the payable amount is `calculate_payout()`, which is pure storage arithmetic (`value - ChildBountiesValuePerParent`) with only a `debug_assert!` (a no-op in production builds) guarding the invariant: [3](#0-2) 

The pallet explicitly documents that `increase_value` (added in `pr_12409`) can push `value` above what the account actually holds, deferring the mismatch to payout time: [4](#0-3) 

This is the same "internal ledger vs. real reserve" divergence as `USDMPegRecovery`: the contract/pallet tracks a value in storage (deposit balances / bounty `value`) and uses it directly for withdrawal/payout math, while the real underlying balance can diverge due to external effects — in the C4 report, LP-token repricing from a swap; here, `increase_value` calls, `Paymaster`/`AssetKind` fee deduction, or partial/failed cross-chain funding legs that leave the sub-account short of the recorded `value`. Once `award_bounty` (or `close_bounty`'s refund path) issues a payment for `value` (or `value - children_value`) that exceeds the actual balance:
- `do_process_payout_payment` → `T::Paymaster::pay(...)` will either fail outright, or (for `Pay` implementations that don't atomically verify the source balance before emitting a payment id) transition the bounty to `PayoutAttempted`/`RefundAttempted` with a payment that can never fully succeed.
- The bounty is now stuck: `check_status` can only retry the same over-committed `value`; there is no path in this pallet to reduce `value` down to the real balance (only `increase_value`, which is monotonic-increasing and requires an `Active` curator), so the child/parent bounty's cleanup (`remove_bounty`) — which is gated on `PaymentState::Succeeded` — can never be reached.

## Impact Explanation
This directly matches the "Public underpriced work / permanent user-fund or bridge-state lock" and "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" impact classes: the parent bounty's recorded value (and by extension the curator deposit, and any funded child-bounty headroom computed from it) becomes permanently unreconcilable with the real balance held in the bounty's derived sub-account, and the funds/administrative state (curator role, deposit, ability to close/award) can become permanently stuck with no governance-free recovery path, exactly as in the H-05 report ("real balances not matching user deposits... culminate with real balances not matching user deposits... inevitable revert").

## Likelihood Explanation
The pallet ships `increase_value` specifically to let value diverge from the account's real balance ("does not check that the bounty account holds new_value"), so the precondition for the mismatch is a documented, first-class, unprivileged-curator-triggerable feature rather than a contrived edge case. Any `AssetKind`/`Paymaster` combination where the amount that lands in the bounty sub-account can be less than the nominal `value` passed to `pay()` (asset transfer fees, XCM remote-asset delivery loss, elastic/rebasing assets) reproduces the same class of drift on the funding side as well, without any curator action required.

## Recommendation
Before authorizing `award_bounty`/`close_bounty` to issue a payment, verify the actual spendable balance of the bounty/child-bounty account (via the relevant `fungibles`/asset `Inspect` balance query for `asset_kind`) is at least the amount about to be paid, and cap/`ensure!` on it rather than trusting `Bounty.value`/`ChildBountiesValuePerParent` blindly; alternatively, replace the `debug_assert!` in `calculate_payout` with a production-enforced `ensure!`/saturating clamp against the live balance, and provide a governance-free path (e.g. a `decrease_value` or `reconcile_value` extrinsic) to bring `value` back in line with the real balance so a bounty is never permanently unrecoverable.

## Proof of Concept
1. Fund a parent bounty with `value = 50`, curator accepts, bounty becomes `Active`.
2. Curator calls `increase_value(parent_bounty_id, 30)` → `Bounty.value = 80` (test `increase_value_creates_child_bounty_headroom` at [5](#0-4)  shows this succeeds with no check on the account's real balance — the bounty account still only holds the originally funded 50, not 80).
3. Curator calls `award_bounty(parent_bounty_id, None, beneficiary)`. `calculate_payout` returns `value` (no active children) = 80, and `do_process_payout_payment` attempts to pay 80 from an account that holds only 50.
4. Depending on `T::Paymaster` semantics this either fails synchronously (bounty stuck in `Active`, curator locked with a re-evaluated, unrecoverable higher deposit) or is accepted as `Attempted` and never resolves to `Succeeded`, permanently blocking `remove_bounty` cleanup and the associated curator-deposit release — a fund/state lock with no path back to a consistent state short of a governance-level storage migration.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L138-154)
```rust
#[derive(Encode, Decode, Clone, PartialEq, Eq, Debug, TypeInfo, MaxEncodedLen)]
pub struct Bounty<AccountId, Balance, AssetKind, Hash, PaymentId, Beneficiary> {
	/// The kind of asset this bounty is rewarded in.
	pub asset_kind: AssetKind,
	/// The amount that should be paid if the bounty is rewarded, including
	/// beneficiary payout and possible child bounties.
	///
	/// The asset class determined by `asset_kind`.
	pub value: Balance,
	/// The metadata concerning the bounty.
	///
	/// The `Hash` refers to the preimage of the `Preimages` provider which can be a JSON
	/// dump or IPFS hash of a JSON file.
	pub metadata: Hash,
	/// The status of this bounty.
	pub status: BountyStatus<AccountId, PaymentId, Beneficiary>,
}
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1002-1033)
```rust
		pub fn award_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			child_bounty_id: Option<BountyIndex>,
			beneficiary: BeneficiaryLookupOf<T, I>,
		) -> DispatchResult {
			let signer = ensure_signed(origin)?;
			let beneficiary = T::BeneficiaryLookup::lookup(beneficiary)?;

			let (asset_kind, value, _, status, _) =
				Self::get_bounty_details(parent_bounty_id, child_bounty_id)?;

			if child_bounty_id.is_none() {
				ensure!(
					ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0,
					Error::<T, I>::HasActiveChildBounty
				);
			}

			let BountyStatus::Active { ref curator } = status else {
				return Err(Error::<T, I>::UnexpectedStatus.into());
			};
			ensure!(signer == *curator, Error::<T, I>::RequireCurator);

			let beneficiary_payment_status = Self::do_process_payout_payment(
				parent_bounty_id,
				child_bounty_id,
				asset_kind,
				value,
				beneficiary.clone(),
				None,
			)?;
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1413-1419)
```rust
		/// - The value can only be increased, never decreased, so the invariant that the sum of
		///   child-bounty values never exceeds the parent value is preserved.
		/// - This call does **not** check that the bounty account holds `new_value`; it only
		///   updates the recorded value. Payouts stay bounded by the account's real balance at
		///   settlement, so increasing the value beyond the available funds simply makes a later
		///   payout fail — no funds are moved by this call.
		/// - Only a parent bounty's value can be increased via this call.
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1644-1661)
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
```

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L2791-2842)
```rust
#[test]
fn increase_value_creates_child_bounty_headroom() {
	ExtBuilder::default().build_and_execute(|| {
		// Given: an active parent bounty (value 50) with its entire value allocated to one child.
		let s = create_active_parent_bounty();
		let _ = Balances::mint_into(&s.curator, 100);
		assert_ok!(Bounties::fund_child_bounty(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			s.value, // 50 — consumes all parent value
			s.metadata,
			None,
		));
		assert_eq!(
			pallet_bounties::ChildBountiesValuePerParent::<Test>::get(s.parent_bounty_id),
			s.value
		);

		// When/Then: no headroom left, another child is rejected.
		assert_noop!(
			Bounties::fund_child_bounty(
				RuntimeOrigin::signed(s.curator),
				s.parent_bounty_id,
				1,
				s.metadata,
				None,
			),
			Error::<Test>::InsufficientBountyValue
		);

		// Given: the parent value is increased, creating new headroom.
		let increase = 30;
		assert_ok!(Bounties::increase_value(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			increase,
		));

		// When/Then: a child funded from the new headroom now succeeds, and the cumulative child
		// value equals the new parent value (invariant Σ child ≤ parent preserved).
		assert_ok!(Bounties::fund_child_bounty(
			RuntimeOrigin::signed(s.curator),
			s.parent_bounty_id,
			increase, // 30
			s.metadata,
			None,
		));
		assert_eq!(
			pallet_bounties::ChildBountiesValuePerParent::<Test>::get(s.parent_bounty_id),
			s.value + increase, // 80
		);
	});
```
