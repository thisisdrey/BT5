Audit Report

## Title
`ChildBountiesValuePerParent` is never decremented when a child bounty is closed without payout, causing the parent bounty to be permanently under-paid - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

## Summary
`calculate_payout` subtracts the total value reserved for children (`ChildBountiesValuePerParent`) from a parent bounty's value to determine the parent beneficiary's payout [1](#0-0) . `remove_bounty`, the sole cleanup routine for child bounty removal, decrements `ChildBountiesPerParent` in the `Some(child_bounty_id)` branch but never decrements or removes the corresponding value from `ChildBountiesValuePerParent`, leaving it fully cleared only in the parent-removal branch [2](#0-1) . This means every child bounty closed without payout permanently inflates the reserved-value accounting for its parent, causing the parent beneficiary to be under-paid by that amount with no dispatchable path to recover the difference.

## Finding Description
The `prdoc` for PR #11425 confirms the team already touched this exact code path, fixing a destructive `take()` bug in `calculate_payout` and moving cleanup logic into `remove_bounty` [3](#0-2) . That fix addressed cleanup only for the full-parent-removal case (`ChildBountiesValuePerParent::<T, I>::remove(parent_bounty_id)` in the `None` branch), but the `Some(child_bounty_id)` branch — reached whenever an individual child bounty is removed, including via `close_child_bounty` before payout — only performs `ChildBountiesPerParent::<T, I>::mutate(parent_bounty_id, |count| count.saturating_dec())` and does not touch `ChildBountiesValuePerParent` at all [2](#0-1) .

Since `close_child_bounty` is a normal curator-callable extrinsic (also invocable by `RejectOrigin`) that leads to child bounty removal via `impl_close_child_bounty` in the sibling `child-bounties` pallet's dispatchable [4](#0-3) , any legitimate closure of a child bounty before it reaches payout leaves `ChildBountiesValuePerParent` stale at its pre-closure value. The subsequent `calculate_payout(parent_bounty_id, None, value)` call then computes `payout = value.saturating_sub(children_value)`, permanently subtracting the closed child's reserved value even though that value was never paid to any child beneficiary [1](#0-0) .

## Impact Explanation
This causes the parent bounty's payout amount to be computed incorrectly (under-paid) and the difference to become permanently stuck in the bounty/treasury source account with no extrinsic path to release it to the rightful beneficiary. This matches the "wrong beneficiary or amount" / "permanent user-fund lock" impact categories for runtime logic that compromises intended payout behavior.

## Likelihood Explanation
The bug is triggered through entirely ordinary, documented pallet usage — a parent curator calling `close_child_bounty` on a child bounty that has not yet reached payout — requiring no privileged governance abuse or attacker-controlled precondition beyond normal curator-level administration of bounties, making it highly reachable and repeatable.

## Recommendation
In the `Some(child_bounty_id)` branch of `remove_bounty`, look up the child bounty's `value` before removal and subtract it from `ChildBountiesValuePerParent::<T, I>` via a saturating or checked mutation whenever the child bounty is removed without a completed payout, mirroring the increment performed at child bounty creation.

## Proof of Concept
1. Create parent bounty `P` with value `V` and fund it.
2. Add child bounty `C` with value `v`, so `ChildBountiesValuePerParent(P) == v`.
3. Curator calls `close_child_bounty(P, C)` before `C` reaches payout, invoking `remove_bounty(P, Some(C), ..)`, which decrements `ChildBountiesPerParent(P)` but leaves `ChildBountiesValuePerParent(P) == v`.
4. Award and claim parent bounty `P`: `calculate_payout(P, None, V)` returns `V - v` instead of `V`.
5. The parent beneficiary receives `V - v`; the remaining `v` is permanently unrecoverable in the source account.

### Citations

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

**File:** substrate/frame/child-bounties/src/lib.rs (L798-814)
```rust
		pub fn close_child_bounty(
			origin: OriginFor<T>,
			#[pallet::compact] parent_bounty_id: BountyIndex,
			#[pallet::compact] child_bounty_id: BountyIndex,
		) -> DispatchResult {
			let maybe_sender = ensure_signed(origin.clone())
				.map(Some)
				.or_else(|_| T::RejectOrigin::ensure_origin(origin).map(|_| None))?;

			// Ensure parent bounty exist, get parent curator.
			let (parent_curator, _) = Self::ensure_bounty_active(parent_bounty_id)?;

			ensure!(maybe_sender.map_or(true, |sender| parent_curator == sender), BadOrigin);

			Self::impl_close_child_bounty(parent_bounty_id, child_bounty_id)?;
			Ok(())
		}
```
