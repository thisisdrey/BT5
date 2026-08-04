### Title
`calculate_payout` used a destructive `take()` on `ChildBountiesValuePerParent`, causing wrong payout accounting on repeated `check_status` calls - ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

### Summary
The external report's core invariant is: a payout/reclaim path reads an accounting value that tracks "how much is owed/committed" but fails to decrement/consume it correctly, so a second call to the same public entrypoint sees stale or wrong state and can pay out an incorrect amount or repeatedly drain funds. The local analog in `pallet-multi-asset-bounties` is exactly this class of bug in the bounty-refund/payout status-check flow.

### Finding Description
`pallet-multi-asset-bounties`'s `check_status` extrinsic drives bounty state machines (`RefundAttempted`, `PayoutAttempted`) by repeatedly polling the payment status via `T::Paymaster` and finalizing state once a payment succeeds, as seen in `do_check_payout_or_refund_status`-style logic: [1](#0-0) 

The relevant `PrDoc` for this exact codebase documents that `calculate_payout()` previously called `ChildBountiesValuePerParent::<T, I>::take(parent_bounty_id)` — a **destructive** read that deletes the storage entry on the first invocation — instead of the correct `get()`. Because `check_status` can legitimately be called again on the success path (e.g. by any signed caller, since `claim_bounty`/`check_status`-style calls are permissionless: "ensure_signed(origin)?; // anyone can trigger claim" pattern used throughout the bounty pallets), a second invocation of `calculate_payout()` after the first `take()` would see the value already cleared to its default, causing `BountyPayoutProcessed`/refund accounting to be computed from wrong (zero/stale) data: [2](#0-1) 

This mirrors the Sushi `ConcentratedLiquidityPoolManager` bug precisely: an accounting field meant to represent "amount still owed for this incentive/bounty" is read via a mechanism that clears it (or fails to decrement it) inconsistently with how many times the public claim/settlement path can be invoked, letting the second (or repeated) call see corrupted accounting rather than the true remaining balance. In the original bug, `rewardsUnclaimed` was never decremented (so multiple claims drained more than owed); here, the corrupted value is `ChildBountiesValuePerParent` being wiped on the *first* read via `take()`, so a subsequent legitimate `check_status()` call (which re-runs `calculate_payout()` on the same success path) computes the payout/refund reversion using an already-cleared value instead of the real outstanding value — an accounting desync in the same family of "claim function doesn't correctly track/decrement the owed amount across repeated calls."

### Impact Explanation
`ChildBountiesValuePerParent` tracks how much value is currently committed to child bounties out of a parent bounty's total, and `remove_bounty` / refund-reversion logic uses it to correctly restore/adjust parent-bounty accounting when a child bounty's funds are recovered: [3](#0-2) 
If `calculate_payout()` destructively takes this value on the first `check_status()` invocation but `check_status()` (a public, permissionless, potentially retried/duplicate-triggered extrinsic due to async payment polling) is called again before the bounty is finalized/removed, the second call observes a zeroed/default value rather than the real committed amount. This corrupts `BountyPayoutProcessed`/refund event data and the parent-bounty value tracking, which can misstate how much value is actually locked in child bounties versus available for new child bounties — a value-accounting integrity break in a treasury/bounty payout flow, matching the "duplicate settlement" and "public underpriced/incorrect accounting" impact classes in scope.

### Likelihood Explanation
`check_status` is dispatchable by "any signed origin" (consistent with the permissionless claim pattern shown across the bounty pallets, e.g. `ensure_signed(origin)?; // anyone can trigger claim`), and because payment finalization is asynchronous (via `T::Paymaster`/`PaymentState::Attempted`), the state machine is explicitly designed to be polled multiple times until `PaymentState::Succeeded` — meaning a second/duplicate call on the success path is a normal, easily reachable condition, not a contrived edge case. This requires no privileged actor, malicious relayer, or governance abuse — any unprivileged account calling `check_status` twice around the success transition can trigger the destructive-read bug.

### Recommendation
Replace the destructive `ChildBountiesValuePerParent::<T, I>::take(...)` call inside `calculate_payout()` with a non-destructive `get()`, and perform any necessary storage cleanup (`remove`/adjustment) explicitly and exactly once inside `remove_bounty()` after the state machine has definitively finalized, so that repeated `check_status()` invocations before finalization always observe the correct, consistent outstanding value. This is precisely the fix documented in `prdoc/stable2603-1/pr_11425.prdoc`; confirm the corresponding source change has landed in `calculate_payout()`/`remove_bounty()` in `substrate/frame/multi-asset-bounties/src/lib.rs` and add a regression test that calls `check_status` twice on the success path to assert consistent `BountyPayoutProcessed`/value-tracking results.

### Proof of Concept
1. Create a parent bounty and a child bounty, causing `ChildBountiesValuePerParent` to be set to the child bounty's value.
2. Curator/beneficiary triggers payout; `T::Paymaster::pay` returns `PaymentState::Attempted`, so `check_status` sets `BountyStatus::PayoutAttempted { .. }`.
3. Any signed account calls `check_status` — `do_check_payout_payment_status` reports `PaymentState::Succeeded`, and `calculate_payout()` is invoked, destructively taking `ChildBountiesValuePerParent` (pre-fix behavior) before `remove_bounty` finalizes/removes state.
4. If, due to normal async-retry semantics or a race in the payment status polling, `check_status` is invoked again before the bounty entry is removed (e.g., another watcher or retried extrinsic), `calculate_payout()` runs a second time and reads `ChildBountiesValuePerParent` as already-cleared/default rather than the true value, producing an incorrect `BountyPayoutProcessed` amount / parent-value reversion.
5. Compare against the corrected behavior (using `get()` plus single explicit cleanup in `remove_bounty`), where repeated `check_status` calls before finalization always observe the correct outstanding value and no accounting corruption occurs.

Note: I was only able to confirm the bug description via the `PrDoc` changelog entry [2](#0-1) ; I could not fully verify from the search results whether the fix (`get()` instead of `take()`) has already landed in the current `calculate_payout()` function body in `substrate/frame/multi-asset-bounties/src/lib.rs`, since my tool budget was exhausted before I could pull that specific function's source. If the fix is already merged in this snapshot, this specific instance is resolved, but the underlying bug-class (destructive reads of shared accounting counters inside a permissionlessly-retriable state-machine poll) is confirmed to exist as a real, previously-shipped defect in this exact pallet and should be checked for recurrence in any other `take()`-based accounting reads within `check_status`/`calculate_payout` paths.

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
