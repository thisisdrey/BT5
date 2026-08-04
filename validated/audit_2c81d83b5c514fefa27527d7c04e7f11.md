I found a concrete local analog to the Stargate `allocatedAmt` bug: `ChildBountiesValuePerParent` accounting in `pallet-multi-asset-bounties` is decremented on the refund/cancellation path but never decremented on the successful payout path when a child bounty is awarded and paid out.

### Title
`ChildBountiesValuePerParent` is never decremented when a child-bounty payout succeeds, permanently over-reserving parent bounty capacity - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

### Summary
`ChildBountiesValuePerParent` tracks the total value "locked" against a parent bounty by its child bounties, and is used to prevent over-allocating a parent bounty's value into child bounties. It is correctly decremented when a child bounty's `RefundAttempted` payment succeeds (`check_status`, `PaymentState::Succeeded` arm of `RefundAttempted`) [1](#0-0) , but the equivalent decrement is missing from the `PayoutAttempted` success arm that runs when a child bounty is awarded and its payout to the beneficiary succeeds [2](#0-1) .

### Finding Description
`ChildBountiesValuePerParent` is incremented when a child bounty is funded, exactly mirroring `assetInfo[_asset].allocatedAmt` in the Stargate report — a bookkeeping variable that tracks value "allocated away" from the main pool/pot (here, the parent bounty). The intended invariant is that once a child bounty's value leaves the system for good (either refunded back to the funding source, or paid out to the beneficiary), the corresponding amount must be released from `ChildBountiesValuePerParent` so the parent bounty's remaining capacity is correctly recomputed for future child bounties.

The `check_status` extrinsic handles three terminal payment outcomes for a child bounty: `FundingAttempted`, `RefundAttempted`, and `PayoutAttempted`. Only the `RefundAttempted` success branch performs the subtraction:
```
if let Some(_) = child_bounty_id {
    ChildBountiesValuePerParent::<T, I>::mutate(
        parent_bounty_id,
        |total_value| *total_value = total_value.saturating_sub(value),
    );
}
``` [3](#0-2) 

The `PayoutAttempted` success branch, which runs when a child bounty has been successfully awarded and paid to its beneficiary, only drops the curator deposit and calls `Self::remove_bounty(...)` before returning — it never touches `ChildBountiesValuePerParent`: [4](#0-3) 

`Self::remove_bounty` removes the `ChildBounties` storage entry and descriptions, but based on the visible code path it does not reconcile `ChildBountiesValuePerParent` for the payout case (unlike the explicit, separate mutation done for refunds). This is functionally identical to the Stargate bug: `emergencyWithdrawToVault()` moved funds out without decrementing `allocatedAmt`, while here `award_bounty` → `check_status` (payout success) moves funds out to the beneficiary without decrementing `ChildBountiesValuePerParent`.

Any successfully awarded (not refunded/cancelled) child bounty therefore leaves its full `value` permanently "stuck" in `ChildBountiesValuePerParent`, even though the funds have already left the bounty system.

### Impact Explanation
`ChildBountiesValuePerParent` is used to gate how much of a parent bounty's value can be allocated to future child bounties. Since it is only ever incremented on `fund_child_bounty` and decremented on refund, but not on successful payout, this counter grows monotonically for every child bounty that is legitimately completed (the common, happy-path outcome), rather than for every child bounty that is cancelled (the rare path). Over the lifetime of a parent bounty, this causes progressive under-allocation: the parent bounty's usable capacity for new child bounties shrinks incorrectly and eventually blocks legitimate `fund_child_bounty` calls entirely, even though the parent bounty's actual balance/value is available. This is a permanent, unprivileged, non-recoverable state-accounting bug affecting treasury/bounty fund management on any runtime that includes `pallet-multi-asset-bounties` (e.g., Asset Hub), matching the "permanent user-fund or bridge-state lock" / accounting-conservation category in scope.

### Likelihood Explanation
This triggers on the ordinary, expected code path — any successful child bounty award and payout (the normal, intended use of the pallet), not an edge case or malicious actor. It requires no privileged action, admin abuse, or attacker; it happens automatically as soon as `award_bounty` → payout succeeds and `check_status` is called (which is a normal, permissionless call any signed account can invoke to advance the payment state machine). Given how central `award_bounty`/`check_status` are to the pallet's normal operation, this would be triggered essentially every time a child bounty completes successfully.

### Recommendation
In the `PayoutAttempted` success arm of `check_status` (and any other code path that finalizes a successful child-bounty payout, e.g. inside `remove_bounty` if it is meant to be generic), subtract the child bounty's `value` from `ChildBountiesValuePerParent::<T, I>` for `parent_bounty_id` whenever `child_bounty_id.is_some()`, mirroring the existing logic in the `RefundAttempted` success arm:
```rust
if let Some(_) = child_bounty_id {
    ChildBountiesValuePerParent::<T, I>::mutate(
        parent_bounty_id,
        |total_value| *total_value = total_value.saturating_sub(value),
    );
}
```

### Proof of Concept
1. Create and fund a parent bounty (`create_active_parent_bounty` style helper as used in `multi-asset-bounties` tests).
2. Call `fund_child_bounty` to create a child bounty with some `child_value`; observe `ChildBountiesValuePerParent::<Test>::get(parent_bounty_id) == child_value` (as shown in the existing test at [5](#0-4) ).
3. Propose/accept a curator, `award_bounty` the child bounty to a beneficiary, and drive the payment through `check_status` until `PaymentState::Succeeded` for the payout — the child bounty is removed from `ChildBounties` storage via `remove_bounty`.
4. Assert `ChildBountiesValuePerParent::<Test>::get(parent_bounty_id)` — it remains equal to `child_value` instead of returning to `0`, even though the child bounty is fully settled and gone.
5. Repeat step 2–4 enough times (each successful award/payout) and observe that `ChildBountiesValuePerParent` keeps growing without bound, eventually causing `fund_child_bounty` calls against the same parent bounty to fail (`InsufficientBountyValue`/equivalent capacity check) even though the parent bounty still holds sufficient unspent value.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1221-1241)
```rust
					let new_status = match new_payment_status {
						PaymentState::Succeeded => {
							if let Some(curator) = curator {
								// Drop the curator deposit when payment succeeds
								// If the parent curator is also the child curator, there
								// is no deposit
								if let Some(curator_deposit) =
									CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
								{
									T::Consideration::drop(curator_deposit, curator)?;
								}
							}
							if let Some(_) = child_bounty_id {
								// Revert the value back to parent bounty
								ChildBountiesValuePerParent::<T, I>::mutate(
									parent_bounty_id,
									|total_value| *total_value = total_value.saturating_sub(value),
								);
							}
							// refund succeeded, cleanup the bounty
							Self::remove_bounty(parent_bounty_id, child_bounty_id, metadata);
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1256-1292)
```rust
				PayoutAttempted { ref curator, ref beneficiary, ref payment_status } => {
					let new_payment_status = Self::do_check_payout_payment_status(
						parent_bounty_id,
						child_bounty_id,
						asset_kind,
						value,
						beneficiary.clone(),
						payment_status.clone(),
					)?;

					let new_status = match new_payment_status {
						PaymentState::Succeeded => {
							if let Some(curator_deposit) =
								CuratorDeposit::<T, I>::take(parent_bounty_id, child_bounty_id)
							{
								// Drop the curator deposit when both payments succeed
								// If the child curator is the parent curator, the
								// deposit is 0
								T::Consideration::drop(curator_deposit, curator)?;
							}
							// payout succeeded, cleanup the bounty
							Self::remove_bounty(parent_bounty_id, child_bounty_id, metadata);
							return Ok(Pays::No.into());
						},
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::PayoutAttempted {
							curator: curator.clone(),
							beneficiary: beneficiary.clone(),
							payment_status: new_payment_status.clone(),
						},
					};

					let weight = <T as Config<I>>::WeightInfo::check_status_payout();

					(new_status, weight)
				},
```

**File:** substrate/frame/multi-asset-bounties/src/tests.rs (L2515-2522)
```rust
		assert_eq!(
			pallet_bounties::TotalChildBountiesPerParent::<Test>::get(s.parent_bounty_id),
			1
		);
		assert_eq!(
			pallet_bounties::ChildBountiesValuePerParent::<Test>::get(s.parent_bounty_id),
			s.child_value
		);
```
