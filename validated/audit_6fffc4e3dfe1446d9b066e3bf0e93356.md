Audit Report

## Title
`ChildBountiesValuePerParent` is never decremented when a child-bounty payout succeeds, permanently over-reserving parent bounty capacity - (File: substrate/frame/multi-asset-bounties/src/lib.rs)

## Summary
`ChildBountiesValuePerParent` is incremented when a child bounty is funded and correctly decremented in the `RefundAttempted` success arm of `check_status`, but the `PayoutAttempted` success arm — reached when a child bounty is successfully awarded and paid out — only drops the curator deposit and calls `Self::remove_bounty` before returning, without decrementing `ChildBountiesValuePerParent`. [1](#0-0) [2](#0-1)  This causes the counter to grow monotonically on every successful (i.e., normal, happy-path) child-bounty completion instead of only on cancellations, eventually blocking legitimate `fund_child_bounty` calls against the parent bounty even though real capacity remains.

## Finding Description
In `check_status`, the `RefundAttempted` success branch explicitly reverts the child bounty's `value` from `ChildBountiesValuePerParent` via `ChildBountiesValuePerParent::<T, I>::mutate(parent_bounty_id, |total_value| *total_value = total_value.saturating_sub(value))` when `child_bounty_id.is_some()`. [3](#0-2) 

The `PayoutAttempted` success branch, reached via the identical `Succeeded` payment-state match arm for the payout path, performs curator-deposit cleanup and calls `Self::remove_bounty(parent_bounty_id, child_bounty_id, metadata)` then returns `Ok(Pays::No.into())` — with no corresponding mutation of `ChildBountiesValuePerParent`. [2](#0-1) 

Both branches call the same `Self::remove_bounty` helper, and the `ChildBountiesValuePerParent` decrement in the refund arm is done explicitly outside of `remove_bounty`, indicating that `remove_bounty` itself does not perform this bookkeeping and it must be done at each call site — which was omitted for the payout path. This is a genuine, reproducible asymmetry in the two otherwise-parallel code paths.

## Impact Explanation
`ChildBountiesValuePerParent` gates how much value can be committed to new child bounties funded from a given parent bounty. Since it is incremented on funding but only decremented on refund/cancellation, every normal successful child-bounty award/payout — the expected, common outcome — permanently inflates this counter relative to the parent bounty's actual remaining balance. Over time this causes progressive under-allocation of legitimate capacity, eventually preventing `fund_child_bounty` from succeeding even though the parent bounty holds sufficient unspent funds. This matches the "permanent user-fund or bridge-state lock" / accounting-conservation category in scope, as it is a state-accounting invariant violation that degrades the pallet's intended fund-management behavior without any privileged actor or attacker required.

## Likelihood Explanation
This triggers on the ordinary, unprivileged, and expected code path: any signed account calling `check_status` to advance a child bounty's payment state machine after a normal `award_bounty` → payout succeeds. No admin privilege, attacker, or edge case is needed — the bug fires on every single successful child-bounty completion, making it highly likely to occur and accumulate in any active deployment of `pallet-multi-asset-bounties`.

## Recommendation
In the `PayoutAttempted` success arm of `check_status`, add the same decrement performed in the `RefundAttempted` arm:
```rust
if let Some(_) = child_bounty_id {
    ChildBountiesValuePerParent::<T, I>::mutate(
        parent_bounty_id,
        |total_value| *total_value = total_value.saturating_sub(value),
    );
}
```
before calling `Self::remove_bounty`, mirroring the existing logic at [3](#0-2) .

## Proof of Concept
1. Create and fund a parent bounty, then call `fund_child_bounty` to create a child bounty with `child_value`; `ChildBountiesValuePerParent::<Test>::get(parent_bounty_id) == child_value` as verified by the existing test. [4](#0-3) 
2. Propose/accept a curator, `award_bounty` the child bounty to a beneficiary, and drive the payment through `check_status` until `PaymentState::Succeeded` for the payout — the branch at [2](#0-1)  executes and removes the child bounty from storage.
3. Assert `ChildBountiesValuePerParent::<Test>::get(parent_bounty_id)` — it remains `child_value` instead of returning to `0`.
4. Repeat funding/awarding additional child bounties against the same parent; observe `ChildBountiesValuePerParent` growing unboundedly until `fund_child_bounty` fails due to perceived insufficient remaining parent bounty capacity, despite real funds being available.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1221-1243)
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
							return Ok(Pays::No.into());
						},
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1266-1279)
```rust
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
