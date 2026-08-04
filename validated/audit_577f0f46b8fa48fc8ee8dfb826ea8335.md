## Title
`ChildBountiesValuePerParent` is never decremented when a child bounty is closed without payout, permanently under-reporting parent bounty payouts - (File: `substrate/frame/multi-asset-bounties/src/lib.rs`)

## Summary
This is a direct structural analog of the FairSide `pendingWithdrawals` bug: a storage counter (`ChildBountiesValuePerParent`) that reserves part of a parent bounty's value against its children is incremented when a child bounty is created but is only ever decremented when the *entire parent bounty* is removed. When an individual child bounty is closed/cancelled (`close_child_bounty`) before reaching payout, the pallet's `remove_bounty` helper decrements the child-count (`ChildBountiesPerParent`) but leaves `ChildBountiesValuePerParent` untouched.

## Finding Description
`calculate_payout` computes what the parent bounty beneficiary receives by subtracting the total reserved child value from the parent bounty's value: [1](#0-0) 

`remove_bounty` is the single cleanup routine invoked whenever a child bounty is removed from storage (e.g. via `close_child_bounty`/`impl_close_child_bounty`, or after successful claim). For the parent-removal branch it correctly clears `ChildBountiesValuePerParent`, but for the child-bounty removal branch it only decrements `ChildBountiesPerParent` — `ChildBountiesValuePerParent` is left as-is: [2](#0-1) 

The pallet's changelog shows the team already found and fixed one closely related bug in this exact function family — `calculate_payout()` used a destructive `take()` on `ChildBountiesValuePerParent`, causing `BountyPayoutProcessed` to report the wrong value on the second call: [3](#0-2) 

That fix moved cleanup into `remove_bounty()`, but it only addressed the "child never removed" scenario checked in the accompanying regression test; it did not add a decrement for the "child bounty closed early" path. This mirrors exactly the FairSide finding: the accounting variable's name/semantics ("value reserved for children of this parent") require it to shrink when a child bounty exits the system without being paid, but no code path does that subtraction outside of full-parent teardown.

## Impact Explanation
When a child bounty is cancelled via `close_child_bounty` (curator-active, funded, or curator-proposed states) rather than paid out, its reserved value keeps counting against the parent in `ChildBountiesValuePerParent`. The parent's eventual `award_bounty`/payout flow (`do_process_payout_payment` → `calculate_payout`) will therefore permanently under-pay the parent beneficiary by the value of every closed-without-payout child bounty, locking the corresponding funds in the bounty/treasury source account with no dispatchable path to recover them for the intended beneficiary. This is a permanent fund-lock / wrong-beneficiary-amount bug in a public payout path, matching the "permanent user-fund lock" and "wrong beneficiary or amount" impact categories in scope.

## Likelihood Explanation
`close_child_bounty` is callable by the parent curator (and forcibly by `RejectOrigin`) as part of normal bounty administration — no attacker-controlled or privileged-abuse precondition is required beyond ordinary curator activity that is expected to happen routinely (a curator changing their mind about a child bounty, or a bounty being cancelled for legitimate reasons before completion). Every such closure silently corrupts `ChildBountiesValuePerParent` for that parent, so the bug is reachable through completely ordinary, unprivileged-adjacent (curator-level, non-admin) usage of a documented feature.

## Recommendation
In the `Some(child_bounty_id)` branch of `remove_bounty`, subtract the child bounty's `value` from `ChildBountiesValuePerParent::<T, I>` (saturating or checked) whenever the child bounty is removed without having completed a successful payout, mirroring the increment performed at child-bounty creation. Add a regression test analogous to the PR #11425 test that closes (not claims) a child bounty and asserts that `calculate_payout` on the parent then returns the full parent value.

## Proof of Concept
1. Create parent bounty `P` with value `V` and fund it.
2. Add a child bounty `C` with value `v` (`ChildBountiesValuePerParent(P) == v`).
3. Curator calls `close_child_bounty(P, C)` before it reaches `PendingPayout` — `remove_bounty(P, Some(C), ..)` runs, decrementing `ChildBountiesPerParent(P)` but leaving `ChildBountiesValuePerParent(P) == v`.
4. Award and claim the parent bounty `P`: `calculate_payout(P, None, V)` computes `payout = V - v`, even though no child bounty ever received `v`.
5. The beneficiary of `P` receives `V - v` instead of `V`; the remaining `v` stays stuck in the bounty account with no call able to release it to the rightful beneficiary. [4](#0-3)

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
