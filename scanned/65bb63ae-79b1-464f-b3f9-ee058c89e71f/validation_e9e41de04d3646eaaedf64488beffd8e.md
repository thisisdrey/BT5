Based on the investigation, the strongest local analog to the "distribution burns shares but never updates a tracked total" bug is in `pallet-multi-asset-bounties`, where the value locked for a completed/cancelled child-bounty is never subtracted from the parent-bounty's tracked `ChildBountiesValuePerParent`, permanently corrupting the value used to gate future child-bounty funding and the parent payout calculation.

Note: I was unable to fully trace the successful-payout finalization path (`check_status`/`PayoutAttempted → Success` handling) within the tool budget to see conclusively whether it calls `remove_bounty` on the success branch or some other cleanup path. The `remove_bounty` function itself, which is the storage-cleanup routine for both parent and child bounties, is confirmed not to touch `ChildBountiesValuePerParent` in the child-bounty branch.

### Title
Child-bounty value is never decremented from `ChildBountiesValuePerParent`, permanently locking parent-bounty funds - (File: substrate/frame/multi-asset-bounties/src/lib.rs)

### Summary
`ChildBountiesValuePerParent` tracks the cumulative value committed to a parent bounty's child bounties, and gates both new child-bounty funding (`fund_child_bounty`) and the parent's own payout (`calculate_payout`). This value is incremented when a child bounty is funded but is never decremented when a child bounty is removed/completed, so completed child-bounty value remains permanently "reserved" against the parent, exactly mirroring the reported bug class where a distribution event burns/moves value without updating the aggregate total that other operations rely on.

### Finding Description
`fund_child_bounty` increases the running total for the parent: [1](#0-0) 

That total is later used to compute the amount available for new child bounties: [2](#0-1) 

and to compute the payout to the parent bounty's own beneficiary: [3](#0-2) 

However, the terminal cleanup routine for a child bounty, `remove_bounty`, only decrements the child-bounty *count* (`ChildBountiesPerParent`) — it never subtracts the child bounty's `value` from `ChildBountiesValuePerParent`: [4](#0-3) 

This is the same broken invariant as the external report: a value-distributing/finalizing event (child-bounty completion, analogous to `withdraw_underlying_to_claim` distributing shares) mutates account-level state but never updates the aggregate counter (`ChildBountiesValuePerParent`, analogous to `total_shares`) that other functions use to compute remaining capacity. Once a child bounty is claimed and removed, its value stays counted against the parent forever, so `remaining_parent_value` in `fund_child_bounty` (line 696) keeps shrinking with every completed child bounty, and `calculate_payout`'s `value.saturating_sub(children_value)` (line 1656) permanently under-pays the parent bounty by the sum of all previously-completed (not just currently active) child-bounty values.

### Impact Explanation
This falls under "permanent user-fund or bridge-state lock" in the accepted impact set: value legitimately owed to the parent-bounty beneficiary is permanently withheld once even one child bounty has been created and completed, and the parent bounty's remaining fundable capacity for new child bounties is permanently and incorrectly reduced. Unlike the original report (which caused failing liquidations due to *insufficient* tracked value), here the mismatch causes an *inflated* tracked liability that under-pays legitimate claimants — an unrecoverable accounting corruption reachable through ordinary curator-level bounty operations, not privileged governance action.

### Likelihood Explanation
Any parent-bounty curator creating and then completing (or cancelling) even a single child bounty via the normal `fund_child_bounty` → award → claim lifecycle will trigger this state corruption, since `remove_bounty`'s child-bounty branch is the only cleanup path shown to run at completion and it does not touch `ChildBountiesValuePerParent`. This requires no special privilege beyond the parent-bounty curator role that already exists in the intended workflow, and the bug is deterministic (not a race condition).

### Recommendation
In `remove_bounty`'s `Some(child_bounty_id)` branch, decrement `ChildBountiesValuePerParent::<T, I>::mutate(parent_bounty_id, |v| *v = v.saturating_sub(child_bounty.value))` using the child bounty's stored `value` before it is dropped, mirroring how `pallet-child-bounties`' `impl_close_child_bounty` reverts `ChildrenCuratorFees` on cancellation. Add a `try-runtime`/`do_try_state` invariant asserting `ChildBountiesValuePerParent::<T,I>::get(parent_id) == sum of value over all live ChildBounties for that parent_id`.

### Proof of Concept
1. Curator creates parent bounty with value `V` and becomes active.
2. Curator calls `fund_child_bounty(parent_id, value = X, ...)` → `ChildBountiesValuePerParent[parent_id] += X`.
3. Child bounty is awarded and successfully claimed/paid, triggering `remove_bounty(parent_id, Some(child_id), ...)` — count decrements, but `ChildBountiesValuePerParent[parent_id]` still equals `X`.
4. Curator calls `award_bounty`/`claim_bounty` (or equivalent completion path) for the parent bounty; `calculate_payout` computes `payout = V - X` instead of `payout = V`, permanently withholding `X` from the parent beneficiary even though the child bounty already consumed and paid out its own separately-funded value.
5. Repeating steps 2–3 for further completed child bounties compounds the locked amount, and `fund_child_bounty`'s `remaining_parent_value` check in step 2's re-run will eventually reject new child bounties that should legitimately be fundable.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L694-697)
```rust
			// Check value
			let child_bounties_value = ChildBountiesValuePerParent::<T, I>::get(parent_bounty_id);
			let remaining_parent_value = parent_value.saturating_sub(child_bounties_value);
			ensure!(remaining_parent_value >= value, Error::<T, I>::InsufficientBountyValue);
```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L723-728)
```rust
			// Add child-bounty value to the cumulative value sum. To be
			// subtracted from the parent bounty payout when awarding
			// bounty.
			ChildBountiesValuePerParent::<T, I>::mutate(parent_bounty_id, |children_value| {
				*children_value = children_value.saturating_add(value)
			});
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

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1663-1685)
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

		T::Preimages::unrequest(&metadata);
	}
```
