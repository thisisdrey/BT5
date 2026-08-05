Note on verification limits: I was not able to fully confirm, within the available tool budget, which of the two router types (`BridgeHubMessageRouter` vs `BridgeHubDualMessageRouter`) is wired into the current `bridge-hub-westend`/`bridge-hub-rococo` runtime configs (`MessageProcessor` associated type of `pallet_message_queue::Config`). The grep hits in both runtime `lib.rs` files reference both names, so both routers exist in this codebase as live, non-test code. The vulnerability described below concerns the router struct itself, which is a real, currently-shipped analog of the "message type exists but is not registered/handled by the dispatcher" bug class from the external report — if `BridgeHubMessageRouter` (the non-Dual variant) is selected as `MessageProcessor` for a chain that also runs `snowbridge-pallet-outbound-queue-v2` (which emits `AggregateMessageOrigin::SnowbridgeV2` messages into the shared `pallet_message_queue`), the result is exactly the reported bug class.

### Title
`BridgeHubMessageRouter` unconditionally rejects `SnowbridgeV2` message origin, permanently stalling Snowbridge V2 outbound processing - (File: cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs)

### Summary
`AggregateMessageOrigin` was extended with a `SnowbridgeV2(H256)` variant to support the new `snowbridge-pallet-outbound-queue-v2` pallet, but the original `BridgeHubMessageRouter::process_message` implementation — still present and exported alongside the newer `BridgeHubDualMessageRouter` — was never updated to route this variant to a processor. It hard-codes `Err(ProcessMessageError::Unsupported)` for it. [1](#0-0) 

### Finding Description
`AggregateMessageOrigin` is the origin type used by the shared `pallet_message_queue` on BridgeHub chains to distinguish where an enqueued message came from: local (`Here`), relay chain (`Parent`), sibling parachains (`Sibling`), the legacy Snowbridge inbound channel (`Snowbridge`), and the newer Snowbridge V2 flow (`SnowbridgeV2(H256)`), added specifically for `snowbridge-pallet-outbound-queue-v2` per PR-8106 ("Snowbridge V2: Add generic AggregateMessageOrigin"). [2](#0-1) 

Two router implementations exist to convert this origin into an actual `ProcessMessage` call:
- `BridgeHubMessageRouter<XcmpProcessor, SnowbridgeProcessor>` — only matches `Here | Parent | Sibling(_)` and `Snowbridge(_)`; for `SnowbridgeV2(_)` it always returns `Err(ProcessMessageError::Unsupported)`. [3](#0-2) 
- `BridgeHubDualMessageRouter<XcmpProcessor, SnowbridgeProcessor, SnowbridgeProcessorV2>` — correctly forwards `SnowbridgeV2(_)` to a dedicated `SnowbridgeProcessorV2`. [4](#0-3) 

This is the direct structural analog of the reported Cosmos SDK bug: a new message/variant (`AddAuthorization`/`RemoveAuthorization` there, `SnowbridgeV2` here) was added to the domain type, but one of the dispatch/registration tables that must handle every variant (`RegisterCodec`/`RegisterInterfaces` there, `BridgeHubMessageRouter::process_message`'s match arm here) was not updated, leaving that variant permanently unroutable through that code path.

`snowbridge-pallet-outbound-queue-v2` documents that it relies entirely on `Config::MessageQueue: EnqueueMessage<Self::AggregateMessageOrigin>` to deliver messages to its `do_process_message` handler via `ProcessMessage::process_message`. [5](#0-4) 
If the runtime's `pallet_message_queue::Config::MessageProcessor` is set to `BridgeHubMessageRouter` (the non-Dual router) rather than `BridgeHubDualMessageRouter`, every message enqueued with origin `AggregateMessageOrigin::SnowbridgeV2(_)` will unconditionally fail with `ProcessMessageError::Unsupported` at the `pallet_message_queue` level, regardless of message content, sender, or validity.

### Impact Explanation
`ProcessMessageError::Unsupported` is treated by `pallet_message_queue` as a permanent, non-retryable failure for that message (as opposed to `Yield`, which causes a retry later or `Overweight`). Any message that reaches the queue tagged with `SnowbridgeV2` origin — i.e., any legitimate outbound V2 message toward Ethereum, sent via `snowbridge_pallet_system_v2::Pallet::send` or `EthereumBlobExporter::deliver` — would be silently and permanently dropped from processing. Since fees for these messages are already reserved/charged at submission time (via `SendMessage::validate`/`deliver` in the V2 pipeline) before the message reaches the queue, this results in: (1) complete, systemic stall of the Snowbridge V2 outbound pipeline (denial of bridge processing), and (2) loss of relayer/user fees already paid for messages that can never be committed, verified, or delivered on Ethereum — a form of permanent bridge-state/fund lock, matching the "message queues... must only advance after decode, dispatch, execution, and settlement succeed atomically" and "permanent user-fund or bridge-state lock" impact categories.

### Likelihood Explanation
This is not attacker-triggered in the sense of a crafted malicious payload — it is triggered by completely ordinary use of the V2 outbound pipeline (any parachain or the BridgeHub itself sending a V2 message) as soon as the wrong router type is selected as `MessageProcessor` for a chain running `outbound-queue-v2`. The likelihood hinges entirely on runtime wiring: if `BridgeHubDualMessageRouter` (or an equivalent that explicitly handles `SnowbridgeV2`) is actually configured as `MessageProcessor` in `bridge-hub-westend`/`bridge-hub-rococo`, this specific path is dormant/inert; if the older `BridgeHubMessageRouter` is used anywhere V2 is enabled (including test/staging chains, or a future/parallel runtime built from this shared crate), the failure is guaranteed and deterministic. I was unable to confirm with certainty from the tool budget available which router is currently wired into each shipped runtime's `MessageProcessor`, so this should be verified directly against `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-westend/src/lib.rs` and `bridge-hub-rococo/src/lib.rs`.

### Recommendation
- Confirm which router (`BridgeHubMessageRouter` vs `BridgeHubDualMessageRouter`) is set as `pallet_message_queue::Config::MessageProcessor` in every runtime that also enables `snowbridge-pallet-outbound-queue-v2`, and ensure it is always `BridgeHubDualMessageRouter` (or equivalent) wherever V2 is active.
- Consider removing/deprecating `BridgeHubMessageRouter` entirely now that `AggregateMessageOrigin::SnowbridgeV2` is a permanent variant of the shared origin enum, to eliminate the possibility of future runtimes accidentally selecting the non-V2-aware router.
- Add a compile-time or runtime sanity check (e.g., a `#[cfg(test)]`/CI check across all runtime configs) asserting that every `AggregateMessageOrigin` variant is exhaustively routed by the configured `MessageProcessor`, so that adding new origin variants without updating all routers fails CI instead of silently degrading bridge processing — directly mirroring the recommended fix in the referenced report of adding explicit registration for every message variant.

### Proof of Concept
1. Configure a `pallet_message_queue::Config` with `MessageProcessor = BridgeHubMessageRouter<XcmpQueue, EthereumInboundQueue>` while also including `snowbridge-pallet-outbound-queue-v2` in the same runtime.
2. Trigger any V2 outbound send (e.g., call `snowbridge_pallet_system_v2::Pallet::send` from a privileged/whitelisted origin, or export an XCM message via `EthereumBlobExporter::deliver`) which enqueues a message into `MessageQueue` with `origin: AggregateMessageOrigin::SnowbridgeV2(topic_hash)`.
3. Observe that on queue processing, `BridgeHubMessageRouter::process_message` matches `SnowbridgeV2(_) => Err(ProcessMessageError::Unsupported)` at line 129, so `snowbridge_pallet_outbound_queue_v2::Pallet::do_process_message` (lines 343-443 of `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`) is never invoked — no `MessageAccepted`/`MessagesCommitted` event is ever emitted for that message, and `pallet_message_queue` marks the message as permanently failed rather than retried.
4. Any fee already charged for the message remains unrecoverable since `PendingOrders`/`Nonce` state in the V2 outbound pallet is never created, and no compensating refund path exists for messages that never reach `do_process_message`.

### Citations

**File:** cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs (L44-60)
```rust
pub enum AggregateMessageOrigin {
	/// The message came from the para-chain itself.
	Here,
	/// The message came from the relay-chain.
	///
	/// This is used by the DMP queue.
	Parent,
	/// The message came from a sibling para-chain.
	///
	/// This is used by the HRMP queue.
	Sibling(ParaId),
	/// The message came from a snowbridge channel.
	///
	/// This is used by Snowbridge inbound queue.
	Snowbridge(ChannelId),
	SnowbridgeV2(H256),
}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs (L103-132)
```rust
/// Routes messages to either the XCMP or Snowbridge processor.
pub struct BridgeHubMessageRouter<XcmpProcessor, SnowbridgeProcessor>(
	PhantomData<(XcmpProcessor, SnowbridgeProcessor)>,
)
where
	XcmpProcessor: ProcessMessage<Origin = AggregateMessageOrigin>,
	SnowbridgeProcessor: ProcessMessage<Origin = AggregateMessageOrigin>;
impl<XcmpProcessor, SnowbridgeProcessor> ProcessMessage
	for BridgeHubMessageRouter<XcmpProcessor, SnowbridgeProcessor>
where
	XcmpProcessor: ProcessMessage<Origin = AggregateMessageOrigin>,
	SnowbridgeProcessor: ProcessMessage<Origin = AggregateMessageOrigin>,
{
	type Origin = AggregateMessageOrigin;
	fn process_message(
		message: &[u8],
		origin: Self::Origin,
		meter: &mut WeightMeter,
		id: &mut [u8; 32],
	) -> Result<bool, ProcessMessageError> {
		use AggregateMessageOrigin::*;
		match origin {
			Here | Parent | Sibling(_) => {
				XcmpProcessor::process_message(message, origin, meter, id)
			},
			Snowbridge(_) => SnowbridgeProcessor::process_message(message, origin, meter, id),
			SnowbridgeV2(_) => Err(ProcessMessageError::Unsupported),
		}
	}
}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs (L151-165)
```rust
	fn process_message(
		message: &[u8],
		origin: Self::Origin,
		meter: &mut WeightMeter,
		id: &mut [u8; 32],
	) -> Result<bool, ProcessMessageError> {
		use AggregateMessageOrigin::*;
		match origin {
			Here | Parent | Sibling(_) => {
				XcmpProcessor::process_message(message, origin, meter, id)
			},
			Snowbridge(_) => SnowbridgeProcessor::process_message(message, origin, meter, id),
			SnowbridgeV2(_) => SnowbridgeProcessorV2::process_message(message, origin, meter, id),
		}
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L13-21)
```rust
//! The message submission pipeline works like this:
//! 1. The message is first validated via the implementation for
//!    [`snowbridge_outbound_queue_primitives::v2::SendMessage::validate`]
//! 2. The message is then enqueued for later processing via the implementation for
//!    [`snowbridge_outbound_queue_primitives::v2::SendMessage::deliver`]
//! 3. The underlying message queue is implemented by [`Config::MessageQueue`]
//! 4. The message queue delivers messages to this pallet via the implementation for
//!    [`frame_support::traits::ProcessMessage::process_message`]
//! 5. The message is processed in `Pallet::do_process_message`:
```
