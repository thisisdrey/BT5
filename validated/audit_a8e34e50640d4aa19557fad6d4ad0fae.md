## Title
XCM instruction errors are collapsed into `ProcessMessageError::Unsupported`, causing transient failures to be permanently dropped by the Message Queue - ([File: polkadot/xcm/xcm-builder/src/process_xcm_message.rs])

### Summary
The bug-class in the external report is a try/catch that only recognizes one narrow error shape (`Error(string)`) and silently mis-handles every other kind of failure, bricking a mechanism meant to gracefully recover. The direct local analog is `ProcessXcmMessage::process_message`, which only special-cases a single XCM instruction error (`ExceedsStackLimit`) as retryable and funnels **every other** `xcm::latest::Error` variant into the generic `ProcessMessageError::Unsupported`, which the Message Queue pallet then treats as a **permanent** failure and drops the message forever.

### Finding Description
`ProcessXcmMessage::process_message` converts an XCM `Outcome::Error` into a `ProcessMessageError` with: [1](#0-0) 

```rust
Outcome::Error(InstructionError { error, index }) => {
    ...
    let error = match error {
        xcm::latest::Error::ExceedsStackLimit => ProcessMessageError::StackLimitReached,
        _ => ProcessMessageError::Unsupported,
    };
    (required, Err(error))
},
```

Only `ExceedsStackLimit` is mapped to the dedicated, context-sensitive `StackLimitReached` variant. `xcm::latest::Error` has dozens of other variants (e.g. `WeightNotComputable`, `Barrier`, `NotHoldingFees`, `TooExpensive`, `Trap`, `FailedToTransactAsset`, etc.), many of which can legitimately occur under transient conditions (temporary weight/fee shortfalls, reentrancy/interpreter limits, momentary asset-transactor failures) rather than because the message itself is malformed or genuinely unsupported. All of them are bucketed into the single `Unsupported` variant.

Downstream, `pallet-message-queue`'s `process_message_payload` treats `Unsupported` (along with `BadFormat`/`Corrupt`) as a **permanent** failure and immediately drops the message: [2](#0-1) 

```rust
Err(error @ BadFormat | error @ Corrupt | error @ Unsupported) => {
    // Permanent error - drop
    Self::deposit_event(Event::<T>::ProcessingFailed { id: id.into(), origin, error });
    MessageExecutionStatus::Unprocessable { permanent: true }
},
```

This is the exact same failure mode as the Solidity report: a narrow allow-list (`Error(string)` there, `ExceedsStackLimit` here) is the only case treated as recoverable/retryable, while everything else that should often be treated as transient is instead swept into a catch-all path with harsher, non-recoverable consequences. The prior fix in this repo (`prdoc/1.12.0/pr_4202.prdoc`, "Treat XCM ExceedsStackLimit errors as transient in the MQ pallet") demonstrates the project has already had to patch exactly one instance of this class, confirming the failure mode is real, but the same generic-`Unsupported` funnel remains for every other XCM execution error. [3](#0-2) 

### Impact Explanation
Any XCM message (including bridge-relayed, reserve-transfer, or teleport instructions carrying asset value) that fails execution for a reason other than stack-depth exhaustion — even a purely transient one such as a fee/weight computation hiccup or a transient asset-transactor failure — is classified as `Unsupported` and permanently dropped by the Message Queue rather than retried. Because dropping happens inside `process_message_payload`'s "permanent" branch, the message can never be resubmitted or manually executed via `execute_overweight`; it is gone. For messages that move value (reserve transfers, teleports, XCM-based bridge deliveries), this can produce a permanent loss/lock of the transferred assets or a stalled bridge delivery, matching the "public underpriced work that degrades block production or stalls bridge processing" / "permanent user-fund or bridge-state lock" impact categories.

### Likelihood Explanation
No privileged actor, relayer misbehavior, or governance action is required — an unprivileged sender can construct or trigger an XCM whose execution legitimately hits any of the many non-`ExceedsStackLimit` error variants (e.g. transient fee/weight or asset-transactor conditions), which under normal network conditions can occur without any malicious intent. The existing precedent (PR 4202) shows this exact mis-classification has already manifested once in production-relevant code, and the fix only covered a single variant instead of reassessing the broader catch-all.

### Recommendation
Do not conflate "instruction not supported/malformed" with "instruction failed for an environment-dependent reason." Extend the match in `ProcessXcmMessage::process_message` to classify additional `xcm::latest::Error` variants that can occur due to transient resource/weight/fee conditions as `ProcessMessageError::Overweight`/`Yield`/`StackLimitReached`-style transient errors instead of collapsing them into `Unsupported`, mirroring the reasoning already applied to `ExceedsStackLimit`.

### Proof of Concept
1. Craft an XCM program that, during `XcmExecutor::execute`, deterministically triggers a non-stack-limit `xcm::latest::Error` variant that is transient in nature (e.g. a weight/fee computation failure that would succeed on retry with more remaining weight/fees available in a later block).
2. Enqueue the message via the Message Queue (`MessageQueue::enqueue_message`).
3. Call `MessageQueue::service_queues` and observe the emitted `Event::ProcessingFailed { error: ProcessMessageError::Unsupported, .. }` followed by permanent removal of the message from `Pages` (as seen in the pattern demonstrated by the existing test `process_discards_stack_ov_message` for the `StackLimitReached` case at `substrate/frame/message-queue/src/tests.rs:1899-1922`, but here for a currently-uncorrected variant), confirming the message cannot be retried or manually executed afterward.

I was unable to fully enumerate every `xcm::latest::Error` variant and confirm from the index which specific ones are genuinely transient in current XCM executor behavior (the full `polkadot/xcm/src/v5/traits.rs` enum listing was only partially retrievable), so I cannot cite a concrete non-`ExceedsStackLimit` variant that is proven transient at the instruction level; this reduces confidence in the exact PoC trigger and should be validated by a Devin session with full codebase/test access.

### Citations

**File:** polkadot/xcm/xcm-builder/src/process_xcm_message.rs (L111-124)
```rust
			Outcome::Error(InstructionError { error, index }) => {
				tracing::trace!(
					target: LOG_TARGET,
					?error,
					?index,
					"XCM message execution error",
				);
				let error = match error {
					xcm::latest::Error::ExceedsStackLimit => ProcessMessageError::StackLimitReached,
					_ => ProcessMessageError::Unsupported,
				};

				(required, Err(error))
			},
```

**File:** substrate/frame/message-queue/src/lib.rs (L1609-1613)
```rust
			Err(error @ BadFormat | error @ Corrupt | error @ Unsupported) => {
				// Permanent error - drop
				Self::deposit_event(Event::<T>::ProcessingFailed { id: id.into(), origin, error });
				MessageExecutionStatus::Unprocessable { permanent: true }
			},
```

**File:** prdoc/1.12.0/pr_4202.prdoc (L1-16)
```text
title: "Treat XCM ExceedsStackLimit errors as transient in the MQ pallet"

doc:
  - audience: Runtime User
    description: |
      Fixes an issue where the MessageQueue can incorrectly assume that a message will permanently fail to process and disallow retrial of it.

crates:
  - name: frame-support
    bump: major
  - name: pallet-message-queue
    bump: patch
  - name: staging-xcm-builder
    bump: patch
  - name: staging-xcm-executor
    bump: patch    
```
