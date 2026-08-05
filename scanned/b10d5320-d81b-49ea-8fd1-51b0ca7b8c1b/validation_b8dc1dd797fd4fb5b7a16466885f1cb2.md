### Title
Non-fatal XCM execution errors are permanently classified as `Unsupported`, causing valid messages (and any assets they carry) to be irrecoverably dropped instead of retried - ([File: polkadot/xcm/xcm-builder/src/process_xcm_message.rs])

### Summary
`ProcessXcmMessage::process_message` maps every `xcm::latest::Error` returned by `XcmExecutor::execute` to `ProcessMessageError::Unsupported`, except for `ExceedsStackLimit` which is mapped to `StackLimitReached`. `pallet-message-queue` treats `Unsupported` (along with `BadFormat`/`Corrupt`) as a **permanent** failure and discards the message forever. This mirrors the GMX bug: a whole class of errors that are not actually "the message is bad" gets funneled into the "drop forever" bucket instead of a retriable bucket, because the classifying function's list of "permanent" vs "transient" cases is incomplete/over-broad.

### Finding Description
`XcmExecutor::execute` can fail with many distinct `xcm::latest::Error` variants (e.g. `Barrier`, `WeightNotComputable`, `NotHoldingFees`, `TooExpensive`, `FeesNotMet`, `Trap`, `ExpectationFalse`, `Overflow`, etc.), most of which reflect transient/contextual conditions (fee market state, weight limits, temporary barrier configuration, transient asset availability) rather than a structurally malformed or unsupported message: [1](#0-0) 

All of these, apart from `ExceedsStackLimit`, are collapsed into `ProcessMessageError::Unsupported` via the wildcard arm `_ => ProcessMessageError::Unsupported`.

`pallet-message-queue::process_message_payload` treats `Unsupported` (together with `BadFormat`/`Corrupt`) as a **permanent** error and discards the message entirely, emitting `ProcessingFailed` and marking `Unprocessable { permanent: true }`: [2](#0-1) 

The `ProcessMessageError` enum itself documents the intended distinction between permanent errors (`BadFormat`, `Corrupt`, `Unsupported` — meant for format/version issues) and transient ones (`Yield`, `Overweight`) that are expected to resolve and be retried: [3](#0-2) 

The XCM processor's wildcard mapping incorrectly places every non-stack-limit XCM execution failure into the permanent `Unsupported` category, even though many of these failures (e.g. `Barrier` — a routing/permission condition that can change after governance or fee-payer state changes, or `TooExpensive`/`NotHoldingFees` — a fee-market condition) are not inherent, permanent properties of the message payload. Once bucketed as `Unsupported`, the message queue never gives the message another chance — it is dropped, and any XCM instructions inside it (which may include `WithdrawAsset`/`DepositAsset`/teleport instructions with already-locked-in provenance on the sending side) are never executed nor reprocessed.

### Impact Explanation
This directly matches the "public underpriced work that degrades block production or stalls bridge/message processing" and "permanent user-fund or bridge-state lock" impact classes: legitimate cross-chain XCM messages (including HRMP/UMP/DMP messages carrying asset transfers) that hit a transient, non-corrupt execution error are permanently discarded by the message queue rather than being retried, resulting in silent, irrecoverable loss of the enclosed operation. Because the mis-classification happens in the generic `ProcessXcmMessage` processor used across the ecosystem (parachains' `on_initialize` XCMP/UMP/DMP processing routed through `pallet-message-queue`), this affects ordinary user-submitted cross-chain messages without requiring any privileged, malicious, or off-chain actor.

### Likelihood Explanation
No privileged actor, validator, collator, or malicious relayer is needed — the condition can be triggered by ordinary XCM execution outcomes (e.g., a `Barrier` rejection due to timing/config, or a fee/weight related failure) that arise during normal cross-chain message processing. Any XCM error other than `ExceedsStackLimit` falls into this incorrect bucket, making the likelihood of hitting the wildcard high for any non-trivial or resource-constrained XCM execution.

### Recommendation
Replace the broad wildcard `_ => ProcessMessageError::Unsupported` with an explicit classification that maps genuinely transient `xcm::latest::Error` variants (e.g., `Barrier`, `WeightNotComputable`, `NotHoldingFees`, `TooExpensive`, `FeesNotMet`) to a retriable outcome (e.g., surfacing them so the message queue can retry rather than permanently discard, analogous to `Yield`/`Overweight`), reserving `Unsupported`/`Corrupt` strictly for cases where the message payload/version itself is structurally invalid or unrecognized.

### Proof of Concept
1. Construct a valid, well-formed XCM message (correct version, correctly decodable) whose execution triggers a transient `xcm::latest::Error` other than `ExceedsStackLimit` — e.g., an instruction that fails with `Error::Barrier` because a `Trader`/barrier condition is momentarily not satisfied, or `Error::TooExpensive` due to current fee-market pricing.
2. Submit the message through the normal HRMP/UMP/DMP path so it is enqueued into `pallet-message-queue` and routed to `ProcessXcmMessage::process_message`.
3. Observe that `XcmExecutor::execute` returns `Outcome::Error(InstructionError { error, .. })`, which the wildcard arm converts to `ProcessMessageError::Unsupported`: [4](#0-3) 
4. Observe in `pallet-message-queue::process_message_payload` that `Unsupported` falls into the permanent-drop arm, emitting `Event::ProcessingFailed` and permanently removing the message from the queue — even though re-processing the identical message moments later (once the transient barrier/fee condition changes) would have succeeded: [5](#0-4) 
5. Any value-moving instructions inside that message (e.g. teleported/reserve-transferred assets already debited on the sending chain) are permanently lost since the message will never be reattempted.

### Citations

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

**File:** substrate/frame/message-queue/src/lib.rs (L1605-1613)
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
```

**File:** substrate/frame/support/src/traits/messages.rs (L34-53)
```rust
pub enum ProcessMessageError {
	/// The message data format is unknown (e.g. unrecognised header)
	BadFormat,
	/// The message data is bad (e.g. decoding returns an error).
	Corrupt,
	/// The message format is unsupported (e.g. old XCM version).
	Unsupported,
	/// Message processing was not attempted because it was not certain that the weight limit
	/// would be respected. The parameter gives the maximum weight which the message could take
	/// to process.
	Overweight(Weight),
	/// The queue wants to give up its current processing slot.
	///
	/// Hints the message processor to cease servicing this queue and proceed to the next
	/// one. This is seen as a *hint*, not an instruction. Implementations must therefore handle
	/// the case that a queue is re-serviced within the same block after *yielding*. A queue is
	/// not required to *yield* again when it is being re-serviced withing the same block.
	Yield,
	/// The message could not be processed for reaching the stack depth limit.
	StackLimitReached,
```
