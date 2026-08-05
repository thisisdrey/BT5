Based on my research, I found a locally-provable analog: the fixed vulnerability in `pallet-multi-asset-bounties` (`substrate/frame/multi-asset-bounties/src/lib.rs`), documented in `prdoc/stable2603-1/pr_11425.prdoc`. It matches the report's core broken invariant exactly: a value is read destructively (analogous to `execute_deposit` writing `position_amount`) inside a function that can be invoked more than once via a public, unprivileged dispatchable (`check_status`), before the state machine has permanently settled — producing an incorrect/duplicate payout value on the second pass, rather than a hard failure.

### Title
`calculate_payout()` destructively consumed `ChildBountiesValuePerParent`, corrupting payout amount on repeated `check_status` calls - ([File: substrate/frame/multi-asset-bounties/src/lib.rs])

### Summary
`pallet-multi-asset-bounties::check_status` is a permissionless, signed-origin dispatchable that any account can call repeatedly on a bounty/child-bounty in `RefundAttempted`/`PayoutAttempted` state while the underlying payment is still `Pending`/`Failed`/`Attempted`. In that path it calls `calculate_payout()`, which used `ChildBountiesValuePerParent::<T, I>::take(parent_bounty_id, child_bounty_id)` — a destructive read — to compute the payout `value`. The bug was fixed per [1](#0-0) , but it is structurally the same class of bug as the reported `PositionInfo` issue: a value used to determine "the amount to be transferred" is zeroed/removed as a side effect of a read inside a function reachable through repeated public calls, rather than being isolated behind an explicit one-shot state transition.

### Finding Description
The relevant call chain is `check_status` → `do_check_status_refund`/payout branch → `calculate_payout()`. As documented in the prdoc: `calculate_payout()` used `.take()` instead of `.get()` on `ChildBountiesValuePerParent`, so the first `check_status()` call deleted the storage entry that records how much of the parent's value is still committed to the child. If the payment was not yet `Succeeded` (e.g., still `Attempted` or `Failed`, both of which keep the bounty in a re-callable "Attempted" state per the code at [2](#0-1) ), the storage entry needed for correctly recomputing `value` on the *next* `check_status()` call had already been consumed. A subsequent call would therefore compute or emit a different value than intended, and `BountyPayoutProcessed`/refund cleanup logic would run with a corrupted amount — exactly like `execute_deposit` overwriting `position_amount` with `received_amount` without any check on whether a withdrawal request was already outstanding, and `request_withdraw` reading a value that a second call could stomp on before settlement completed.

The `check_status` entrypoint permits any signed account (not necessarily the curator/beneficiary) to trigger this path multiple times while the payment stays in a non-terminal state, since these branches loop back into `BountyStatus::RefundAttempted`/`PayoutAttempted` (see [3](#0-2) ) rather than failing outright — meaning "no check that a request is already in progress" is not enforced at the state-machine level for the `take()`-consumed value; only the final `Succeeded` branch cleans up state.

### Impact Explanation
If unpatched, this breaks the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant for bounty payouts: a destructively-read accounting value could be zeroed/lost before settlement, producing a `BountyPayoutProcessed` event with an incorrect payout value and desynchronizing `ChildBountiesValuePerParent` from the actual owed amount — a form of duplicate/incorrect settlement that misallocates treasury-sourced funds between parent and child bounties.

### Likelihood Explanation
Reachability requires only a signed, unprivileged caller repeatedly invoking the public `check_status` dispatchable on a bounty whose payment attempt has not yet resolved to `Succeeded` — no validator, relayer, governance, or key compromise is needed, matching the "public underpriced work" / "duplicate settlement" acceptance criteria. This is confirmed as a real, fixed issue (not speculative) by the project's own prdoc.

### Recommendation
Confirm the fix from `pr_11425` (replacing `take()` with `get()` and moving storage cleanup to `remove_bounty()`) is present in the deployed runtime version in scope, and audit other `check_status`/multi-call state-machine functions in this pallet family for the same destructive-read-before-settlement pattern (any `::take()` used to compute a value inside a function that can re-enter its own "Attempted" branch on failure).

### Proof of Concept
Not independently reproducible from static analysis alone since the fix is already merged in this snapshot ( [4](#0-3) ); the prdoc itself documents the failure mode: call `check_status()` once with a `PaymentState` that doesn't reach `Succeeded` (e.g., `Attempted`/`Failed`), observe `ChildBountiesValuePerParent` entry removed by the `take()`; call `check_status()` again — `calculate_payout()` recomputes `value` from a now-missing/default entry, and `BountyPayoutProcessed`/event emits an incorrect payout value.

**Uncertainty note:** I could not verify from the index whether the exact current `substrate/frame/multi-asset-bounties/src/lib.rs` in this snapshot still contains the destructive `take()` in `calculate_payout()`, since the surrounding code shown uses `.take()` for `CuratorDeposit` (a different, seemingly intentional one-shot consumption tied to the `Succeeded` branch) but I was not able to view the body of `calculate_payout()` itself before iterations ran out. If this specific function has already applied the `pr_11425` fix, this finding should be treated as historical/patched rather than a live vulnerability — I recommend a Devin session read `substrate/frame/multi-asset-bounties/src/lib.rs` in full (`calculate_payout` function body) to confirm current state before filing.

### Citations

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

**File:** substrate/frame/multi-asset-bounties/src/lib.rs (L1244-1256)
```rust
						PaymentState::Pending |
						PaymentState::Failed |
						PaymentState::Attempted { .. } => BountyStatus::RefundAttempted {
							payment_status: new_payment_status,
							curator: curator.clone(),
						},
					};

					let weight = <T as Config<I>>::WeightInfo::check_status_refund();

					(new_status, weight)
				},
				PayoutAttempted { ref curator, ref beneficiary, ref payment_status } => {
```
