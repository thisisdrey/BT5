## Title
`ChildBountiesValuePerParent` is never decremented when a child bounty completes, permanently starving future child-bounty funding - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

## Summary
This is a direct structural analog of the Astaria H-16 bug class: a per-owner accounting counter is incremented when a sub-obligation is created but is never decremented when that sub-obligation is settled/removed, because the removal code path only updates a *different* counter. In `pallet-multi-asset-bounties`, `ChildBountiesValuePerParent` tracks the cumulative value committed to a parent bounty's child bounties and gates all future `fund_child_bounty` calls. When a child bounty finishes (payout completes or is cancelled/refunded) and is removed from storage, only `ChildBountiesPerParent` (the *count*) is decremented in `remove_bounty` — `ChildBountiesValuePerParent` (the *value*) is left untouched. Value committed to completed child bounties is thus permanently "stuck" against the parent's budget, exactly like Astaria's `liensOpenForEpoch` counter that was decremented on one side of a lien transfer but never incremented/reconciled on the other, corrupting the accounting invariant for the life of the vault/bounty.

## Finding Description
`fund_child_bounty` gates new child bounties on `remaining_parent_value`, computed as `parent_value - ChildBountiesValuePerParent::get(parent_bounty_id)`: [1](#0-0) 

On success it *adds* the new child value to `ChildBountiesValuePerParent` and increments `ChildBountiesPerParent`: [2](#0-1) 

When a child bounty is later removed (after being fully paid out via `PayoutAttempted` completion, or refunded via `close_bounty`/cancellation), `remove_bounty` is invoked: [3](#0-2) 

In the `Some(child_bounty_id)` branch, only `ChildBountiesPerParent` is decremented (`count.saturating_dec()`). There is no corresponding subtraction from `ChildBountiesValuePerParent`. This mirrors the Astaria root cause precisely: the pallet has two coupled counters that must move in lockstep on both "gain" and "loss" of a child obligation (count and value, analogous to Astaria's slope/yIntercept and `liensOpenForEpoch`), but the removal path only updates one of them. The other remains permanently inflated by the value of every child bounty that has ever completed, even though those funds have already left the parent bounty's balance and are no longer "at risk."

## Impact Explanation
Because `ChildBountiesValuePerParent` is monotonically non-decreasing across the parent bounty's lifetime (it only grows, on every `fund_child_bounty` call, and never shrinks on completion), `remaining_parent_value` in `fund_child_bounty` becomes permanently understated after the first child bounty completes. Eventually `ChildBountiesValuePerParent` accumulates to equal or exceed `parent_value`, at which point `fund_child_bounty` unconditionally fails with `InsufficientBountyValue` for *every* subsequent call — regardless of how much uncommitted balance actually remains in the parent bounty account. This permanently locks legitimate spend capacity of the parent bounty (a "permanent user-fund ... lock" per the impact gate), denying the curator the ability to create further child bounties even though on-chain funds are available and unclaimed, degrading the bounty/treasury payout flow for the life of the parent bounty.

## Likelihood Explanation
This requires no privileged actor, malicious peer, or governance abuse — a single ordinary curator funding, awarding/closing, and re-funding child bounties under normal pallet operation triggers the bug deterministically. Any parent bounty that completes more than a small number of child bounties over its lifetime (cumulative completed value approaching `parent_value`) will hit this ceiling, so likelihood of triggering the invariant break under normal usage is high.

## Recommendation
In `remove_bounty` (and any other place a child bounty is fully settled/removed), subtract the removed child bounty's `value` from `ChildBountiesValuePerParent::<T, I>` (mirroring the existing decrement of `ChildBountiesPerParent`) using `saturating_sub`, so that completed/refunded child-bounty value is released back to the parent's spendable budget, keeping the count and value trackers synchronized on both increment and decrement paths.

## Proof of Concept
1. Create and fund a parent bounty with `value = V`.
2. Curator calls `fund_child_bounty` repeatedly with small values that sum close to `V`; each call increments `ChildBountiesValuePerParent` by that value (`lib.rs:723-737`).
3. Each child bounty is awarded and successfully paid out to completion, ultimately calling `remove_bounty(parent_bounty_id, Some(child_bounty_id), ..)` (`lib.rs:1663-1685`), which decrements only `ChildBountiesPerParent`, leaving `ChildBountiesValuePerParent` unchanged.
4. Even though every previously funded child bounty has been fully paid and removed (so the parent's actual committed balance for pending child bounties is now `0`), `ChildBountiesValuePerParent::get(parent_bounty_id)` still reflects the sum of all historical child bounty values.
5. The curator calls `fund_child_bounty` again for a new legitimate child bounty; `remaining_parent_value = parent_value.saturating_sub(child_bounties_value)` (`lib.rs:695-696`) is now much smaller than reality (or zero), and the call reverts with `Error::InsufficientBountyValue` (`lib.rs:697`) despite the parent bounty holding sufficient uncommitted funds.

### Citations

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L694-698)
```rust
			// Check value
			let child_bounties_value = ChildBountiesValuePerParent::<T, I>::get(parent_bounty_id);
			let remaining_parent_value = parent_value.saturating_sub(child_bounties_value);
			ensure!(remaining_parent_value >= value, Error::<T, I>::InsufficientBountyValue);

```

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L723-737)
```rust
			// Add child-bounty value to the cumulative value sum. To be
			// subtracted from the parent bounty payout when awarding
			// bounty.
			ChildBountiesValuePerParent::<T, I>::mutate(parent_bounty_id, |children_value| {
				*children_value = children_value.saturating_add(value)
			});

			// Increment the active child-bounty count.
			ChildBountiesPerParent::<T, I>::mutate(parent_bounty_id, |count| {
				count.saturating_inc()
			});
			TotalChildBountiesPerParent::<T, I>::insert(
				parent_bounty_id,
				child_bounty_id.saturating_add(1),
			);
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
