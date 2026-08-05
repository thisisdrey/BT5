Audit Report

## Title
Permanently blocked bounty beneficiary can lock a parent bounty forever via `ChildBountiesPerParent` - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

## Summary
`pallet-multi-asset-bounties` requires `ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0` before a parent bounty can be closed [1](#0-0)  or awarded [2](#0-1) , and this counter is decremented only inside `remove_bounty`, reached solely on the `PaymentState::Succeeded` branches of `check_status` [3](#0-2) [4](#0-3) . If a child bounty's beneficiary payout can never succeed (e.g., a frozen/blocked asset account), the child bounty remains stuck in `PayoutAttempted` on every `check_status`/`retry_payment` call, and the parent bounty can never be closed or awarded.

## Finding Description
The code paths cited in the claim are accurate as reviewed: `award_bounty` gates on `ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0` when `child_bounty_id.is_none()` [2](#0-1) , and `close_bounty` has the identical gate [1](#0-0) . In `check_status`, the `PayoutAttempted` branch only calls `remove_bounty` (which decrements `ChildBountiesPerParent`) when `new_payment_status == PaymentState::Succeeded`; any `Pending`, `Failed`, or `Attempted` state simply re-writes the same `PayoutAttempted` status and returns [5](#0-4) . `retry_payment` for the `PayoutAttempted` case likewise just calls `do_process_payout_payment` again and rewrites `PayoutAttempted` with whatever new payment status comes back [6](#0-5) . I found no `force_close`, `force_remove`, `AdminOrigin`, or `RejectOrigin`-gated recovery call in this pallet — a grep across the pallet file for these terms only matched the `Config` trait's own origin-type name references, not any escape-hatch extrinsic. This confirms there is no privileged administrative path in the reviewed pallet code to forcibly clear a child bounty stuck in `PayoutAttempted`, matching the claim that the only decrement point is the success branch of `remove_bounty`.

I was not able to fully read the bodies of `do_check_payout_payment_status` and `do_process_payout_payment` (only their call sites and signatures were located within the available tool budget), nor the concrete `T::Paymaster` implementation wired into a production runtime to confirm that a frozen/blocked beneficiary account causes `Paymaster::pay` to return a permanently non-succeeding state rather than an immediate `Err` that would abort the call before `PayoutAttempted` is even set. This is the same gap the original claim author flagged. Based on the general `PaymentStatus`/pay-and-poll pattern used elsewhere in Substrate (e.g., `frame_support::traits::tokens::Pay`), a transfer that is rejected by the destination account typically surfaces as a `Failed`/non-succeeding poll result rather than a synchronous `pay()` error, which is consistent with the claim, but I could not confirm this end-to-end within the given budget.

## Impact Explanation
If the mechanism holds as traced (which the reviewed code strongly supports), a single uncooperative or permanently-blocked beneficiary can indefinitely lock a parent bounty's `close_bounty`/`award_bounty` entrypoints via the `ChildBountiesPerParent` counter never reaching zero, matching the "permanent user-fund or bridge-state lock" impact category. This requires no privileged action and no dependency-only issue — it is a reachable state from a normal curator `award_bounty` call to a beneficiary account whose asset acceptance is blocked/frozen.

## Likelihood Explanation
Likelihood depends on whether `T::Paymaster` is backed by an asset system that supports frozen/blocked destination accounts (e.g., `pallet-assets`), and whether `do_check_payout_payment_status`/`do_process_payout_payment` treat a destination-side rejection as a retryable-but-never-succeeding `Failed`/`Pending` poll result instead of aborting the transition into `PayoutAttempted` in the first place. The reviewed dispatch logic in `award_bounty`, `check_status`, and `retry_payment` is consistent with the claimed deadlock, but confirmation of the exact payment-status semantics inside `do_check_payout_payment_status`/`do_process_payout_payment` and the concrete `Paymaster` binding used by a live runtime instance of this pallet could not be completed with the available tools.

## Recommendation
As proposed in the claim: add an administrative/timeout-based path to force-remove or write off a child bounty permanently stuck in `PayoutAttempted`/`RefundAttempted` (decrementing `ChildBountiesPerParent` without requiring payment success), or allow redirecting a permanently-failing payout to an alternate beneficiary so `remove_bounty` can eventually execute.

## Proof of Concept
1. Create a parent bounty and a child bounty using an asset kind serviced by a `Paymaster` backed by `pallet-assets`.
2. As curator, call `award_bounty(parent_id, Some(child_id), beneficiary)` where `beneficiary` is frozen/blocked for that asset.
3. `do_process_payout_payment` initiates the payment; the status transitions to `PayoutAttempted`.
4. Repeatedly call `check_status(parent_id, Some(child_id))` and `retry_payment(parent_id, Some(child_id))` — per the code at [5](#0-4) , the status never advances past `PayoutAttempted` unless `PaymentState::Succeeded` is reached.
5. `ChildBountiesPerParent::<T, I>::get(parent_id)` remains nonzero forever.
6. `close_bounty(parent_id, None)` and `award_bounty(parent_id, None, ...)` fail permanently with `Error::<T, I>::HasActiveChildBounty` per [1](#0-0)  and [2](#0-1) .

Note: confirming step 3's actual failure semantics requires reading `do_check_payout_payment_status`/`do_process_payout_payment` bodies and the concrete `Paymaster` implementation used in a deployed runtime, which was not completed within the available investigation budget — this is a genuine open gap in verification, not a refutation of the claim.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1013-1018)
```rust

			if child_bounty_id.is_none() {
				ensure!(
					ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0,
					Error::<T, I>::HasActiveChildBounty
				);
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1099-1105)
```rust
			match child_bounty_id {
				None => {
					// Parent bounty can only be closed if it has no active child bounties.
					ensure!(
						ChildBountiesPerParent::<T, I>::get(parent_bounty_id) == 0,
						Error::<T, I>::HasActiveChildBounty
					);
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

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1372-1389)
```rust
				PayoutAttempted { ref curator, ref beneficiary, ref payment_status } => {
					let new_payment_status = Self::do_process_payout_payment(
						parent_bounty_id,
						child_bounty_id,
						asset_kind,
						value,
						beneficiary.clone(),
						Some(payment_status.clone()),
					)?;
					(
						PayoutAttempted {
							curator: curator.clone(),
							beneficiary: beneficiary.clone(),
							payment_status: new_payment_status,
						},
						<T as Config<I>>::WeightInfo::retry_payment_payout(),
					)
				},
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1663-1682)
```rust
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
```
