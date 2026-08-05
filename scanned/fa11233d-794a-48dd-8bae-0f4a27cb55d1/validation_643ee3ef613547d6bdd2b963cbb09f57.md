## Analysis Summary

The bug-class from the report is: **an attacker-influenced external call that fails in a way that gets misclassified as a permanent, unrecoverable failure, so the surrounding operation (and any value already committed to it) is permanently bricked/lost, with no retry path.**

The closest local analog is in the interaction between the XCM message processor and `pallet-message-queue`.

### Title
Overly broad `Unsupported` error mapping in `ProcessXcmMessage` causes `pallet-message-queue` to permanently and irrecoverably drop XCM messages (and any value they carry) instead of retrying — (`File: polkadot/xcm/xcm-builder/src/process_xcm_message.rs`)

### Summary
`ProcessXcmMessage::process_message` is the `MessageProcessor` used by `pallet-message-queue` for DMP/UMP/HRMP-style XCM queues across the relay chain and parachain runtimes. It converts every `Outcome::Error` variant except `ExceedsStackLimit` into `ProcessMessageError::Unsupported`, and every `prepare()` failure directly into `ProcessMessageError::Unsupported` as well. `pallet-message-queue` treats `Unsupported` identically to `Corrupt`/`BadFormat` — i.e. as a **permanent** failure, dropping the message forever with no retry mechanism, unlike `Overweight` messages which can be re-executed manually via `execute_overweight`.

### Finding Description
`ProcessXcmMessage::process_message` (`polkadot/xcm/xcm-builder/src/process_xcm_message.rs:44-128`) first calls `XcmExecutor::prepare(message, Weight::MAX)`; any failure there is mapped straight to `ProcessMessageError::Unsupported`: [1](#0-0) 

If `prepare()` succeeds but `XcmExecutor::execute` returns `Outcome::Error`, the code special-cases only `ExceedsStackLimit`; every other XCM `Error` variant (there are ~30, e.g. `WeightNotComputable`, `Transport`, `Unroutable`, `HoldingWouldOverflow`, etc.) is collapsed into the same `Unsupported`: [2](#0-1) 

`pallet-message-queue`'s `process_message_payload` treats `Unsupported` (along with `BadFormat`/`Corrupt`) as `MessageExecutionStatus::Unprocessable { permanent: true }`, which is a terminal drop with an `Event::ProcessingFailed` and **no retry path** — contrast this with `Overweight`, which is recoverable via `execute_overweight`, and `Yield`, which is retried automatically: [3](#0-2) 

The pallet's own documentation confirms the intended severity split between transient/permanent classifications, and a prior fix (`prdoc/1.12.0/pr_4202.prdoc`) explicitly acknowledges that `MessageQueue` "can incorrectly assume that a message will permanently fail to process and disallow retrial of it" — but the fix only special-cased `ExceedsStackLimit`, leaving the blanket `_ => Unsupported` catch-all for every other `Outcome::Error` and for every `prepare()` failure: [4](#0-3) 

The critical detail (paralleling the OpenQ bug) is *when* an XCM message reaches this processor: for DMP/UMP/HRMP transfers, the sending chain has already **irrevocably burned, reserved, or locked** the corresponding assets in a prior, separate transaction before the message is even queued. If the receiving chain's `prepare()`/`execute()` step for that message falls into any of the many non-stack-limit error branches — because of an attacker-crafted message shape (unweighable nested `SetErrorHandler`/`SetAppendix`, oversized `AssetTransactor` interaction, a temporarily-unavailable pallet dependency, etc.) — the message is discarded permanently with `permanent: true`, `Event::ProcessingFailed` fires, and the corresponding value that the sender chain has already committed to the transfer is never credited on the receiver, is never trapped by `AssetTrap` (since the executor didn't get far enough to hold the assets), and cannot be retried. This is a “poison message” that consumes its one processing attempt and disappears, exactly mirroring how the malicious ERC-20 in OpenQ consumed the bounty's claim path and bricked it permanently.

### Impact Explanation
This maps to the Impact Gate's "permanent user-fund or bridge-state lock" and "message queues... must only advance after decode, dispatch, execution, and settlement succeed atomically" pivots: a message that fails for a reason other than the one carved-out transient case is unconditionally treated as settled/dropped by the queue, even though the paired settlement (asset credit, XCM side effect, cross-chain delivery) never happened. Any DMP/UMP/HRMP message an attacker can shape to trigger a non-`ExceedsStackLimit` `Outcome::Error`, or any `prepare()`-time weighing failure, causes silent, permanent, unrecoverable loss of the message's intended effect with no compensating mechanism — a strictly worse outcome than the recoverable `Overweight` path the pallet already provides for a similar class of problem.

### Likelihood Explanation
No privileged actor, validator, relayer, or governance action is required. Any account able to originate a cross-consensus message that gets routed through `pallet-message-queue` via `ProcessXcmMessage` (e.g. any user or parachain sending an XCM through `pallet-xcm::send`, or standard XCMP/UMP/DMP traffic) can shape the XCM payload/instructions to hit an error branch outside the single carved-out `ExceedsStackLimit` case. Given how many distinct `xcm::latest::Error` variants exist and how coarse the `_ =>` fallback is, this is easy to trigger deliberately and is likely to occur unintentionally as well, which is itself evidence the guard is insufficient (the prior `ExceedsStackLimit` incident was found in production usage, not by adversarial testing).

### Recommendation
Classify `Outcome::Error` variants individually instead of collapsing them into a single `Unsupported` bucket; only truly non-retryable conditions (malformed/corrupt data) should map to `permanent: true`. Errors that are plausibly transient (e.g. `WeightNotComputable`, dependency-not-ready conditions, sender/queue congestion errors) should map to `Yield`/temporary classifications so `pallet-message-queue` retries them, matching the treatment already given to `ExceedsStackLimit`. Additionally, `prepare()` failures should not be unconditionally treated as permanent without inspecting the underlying `InstructionError`.

### Proof of Concept
Conceptual reproduction path (grounded in the cited code, not independently executed):
1. Construct an XCM message whose weighing step in `Config::Weigher::weight` fails deterministically for reasons other than the fixed `ExceedsStackLimit` case (e.g. deeply nested `SetAppendix`/`SetErrorHandler` combinations that are individually weighable but interact to break the weigher, or any instruction sequence documented to legitimately trigger `xcm::latest::Error::WeightNotComputable`/other variants at `execute()` time).
2. Have this message queued via DMP/UMP/HRMP into `pallet-message-queue` for a target chain, where the corresponding value transfer/reserve was already committed on the sending side.
3. Observe `ProcessXcmMessage::process_message` return `ProcessMessageError::Unsupported`.
4. Observe `pallet-message-queue::process_message_payload` classify this as `MessageExecutionStatus::Unprocessable { permanent: true }`, emit `Event::ProcessingFailed`, and permanently drop the message — with no `execute_overweight`-style recovery mechanism and no corresponding asset trap or credit having occurred on the receiving side.

**Uncertainty note:** I was not able to fully enumerate, within the available code excerpts, every runtime code path that produces `Outcome::Error` (vs. `Outcome::Incomplete`) at the `vm.post_process` stage — I could not retrieve that function's body. The `prepare()`-failure path (mapped directly to `Unsupported`) is fully confirmed from the cited code. Further verification of exactly which `Outcome::Error` variants are practically reachable by an unprivileged sender at runtime would benefit from reading `XcmExecutor::post_process` directly, which a Devin session with full repo access could confirm.

### Citations

**File:** polkadot/xcm/xcm-builder/src/process_xcm_message.rs (L71-78)
```rust
		let pre = XcmExecutor::prepare(message, Weight::MAX).map_err(|_| {
			tracing::trace!(
				target: LOG_TARGET,
				"Failed to prepare message.",
			);

			ProcessMessageError::Unsupported
		})?;
```

**File:** polkadot/xcm/xcm-builder/src/process_xcm_message.rs (L110-124)
```rust
			// In the error-case we assume the worst case and consume all possible weight.
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

**File:** substrate/frame/message-queue/src/lib.rs (L1605-1617)
```rust
			Err(Yield) => {
				// Processing should be reattempted later.
				MessageExecutionStatus::Unprocessable { permanent: false }
			},
			Err(error @ BadFormat | error @ Corrupt | error @ Unsupported) => {
				// Permanent error - drop
				Self::deposit_event(Event::<T>::ProcessingFailed { id: id.into(), origin, error });
				MessageExecutionStatus::Unprocessable { permanent: true }
			},
			Err(error @ StackLimitReached) => {
				Self::deposit_event(Event::<T>::ProcessingFailed { id: id.into(), origin, error });
				MessageExecutionStatus::StackLimitReached
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
