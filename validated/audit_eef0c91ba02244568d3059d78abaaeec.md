### Title
`BridgeHubMessageRouter` cannot process `SnowbridgeV2` inbound messages, permanently stalling the bridge message queue - (File: `cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs`)

### Summary
This is the direct on-chain analog of the C4 finding: `ArbitrumCoreBranchRouter.executeNoSettlement` was a router that dispatched on a discriminant (a function selector) but was never updated to add a new branch (`0x07`) that its sibling implementation already handled. In `polkadot-sdk`, `BridgeHubMessageRouter` is a `ProcessMessage` router that dispatches on the `AggregateMessageOrigin` discriminant but was never updated to actually process the `SnowbridgeV2` branch that its sibling `BridgeHubDualMessageRouter` already handles.

### Finding Description
`AggregateMessageOrigin` is the shared origin enum used by `pallet_message_queue` on BridgeHub chains, and it has five variants: `Here`, `Parent`, `Sibling`, `Snowbridge`, and `SnowbridgeV2` [1](#0-0) .

Two `ProcessMessage` router implementations exist for dispatching queued messages by origin:

- `BridgeHubMessageRouter<XcmpProcessor, SnowbridgeProcessor>` matches on `Here | Parent | Sibling(_)` → `XcmpProcessor`, `Snowbridge(_)` → `SnowbridgeProcessor`, but for `SnowbridgeV2(_)` it explicitly returns `Err(ProcessMessageError::Unsupported)` instead of dispatching to any processor [2](#0-1) .
- `BridgeHubDualMessageRouter<XcmpProcessor, SnowbridgeProcessor, SnowbridgeProcessorV2>` was added later and correctly routes `SnowbridgeV2(_)` to a dedicated `SnowbridgeProcessorV2` [3](#0-2) .

This mirrors the reported bug class precisely: when `SnowbridgeV2` support (the inbound-queue-v2 pallet, added per `pr_8175.prdoc` for "Snowbridge V2: Generic inbound message processing") was introduced, the original `BridgeHubMessageRouter` was left unmodified — it still only understands the pre-V2 variant set, exactly as `ArbitrumCoreBranchRouter.executeNoSettlement` was left unmodified when `0x07` support was added to `CoreBranchRouter`.

### Impact Explanation
If any BridgeHub runtime configuration wires `pallet_message_queue`'s `MessageProcessor` to `BridgeHubMessageRouter` (the non-`Dual` variant) while also enabling `snowbridge-pallet-inbound-queue-v2` (which enqueues messages under the `SnowbridgeV2` origin), every message queued under that origin will hit `Err(ProcessMessageError::Unsupported)` on every processing attempt. In `pallet_message_queue`, an `Unsupported` result is treated as a permanent processing failure for that message/origin, so V2 Snowbridge messages would be stuck forever, unable to be delivered to AssetHub. This fits the "message queues... must only advance after decode, dispatch, execution, and settlement succeed atomically" and "public underpriced work that degrades block production or stalls bridge processing" impact classes — it is a permanent bridge-state lock for an entire message channel, reachable without any privileged actor once a relayer submits a legitimate V2 message.

### Likelihood Explanation
I was not able to fully confirm, within the available search budget, whether the currently deployed `bridge-hub-westend` / `bridge-hub-rococo` runtimes actually configure `pallet_message_queue`'s processor as the plain `BridgeHubMessageRouter` versus the `Dual` variant that correctly supports `SnowbridgeV2` — both routers are referenced in `cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/lib.rs` and `.../bridge-hub-westend/src/lib.rs`, but I could not verify the exact `MessageProcessor` type wired into the pallet's `Config` before running out of iterations. If the non-`Dual` router is the one actually configured in any live/upgradeable runtime that also has `inbound-queue-v2` active, the likelihood is high and the trigger is trivial (any inbound V2 message). If only the `Dual` router is used wherever V2 is enabled, this is dead/unreachable code and the practical risk is limited to future misconfiguration (e.g., forgetting to switch to the `Dual` router when enabling `inbound-queue-v2` on a new chain, exactly as the original finding describes a forgotten-update scenario).

### Recommendation
- Either remove the plain `BridgeHubMessageRouter` (forcing all configurations to use `BridgeHubDualMessageRouter`, which exhaustively matches every `AggregateMessageOrigin` variant), or
- Make `BridgeHubMessageRouter` generic and forward `SnowbridgeV2` to a required processor type (mirroring the `Dual` router) rather than silently returning `Unsupported`, and
- Add a runtime/CI check (e.g., an exhaustive match without a wildcard arm, already the case here) plus an integration test that asserts every `AggregateMessageOrigin` variant is routed to a processor for every BridgeHub runtime that enables `inbound-queue-v2`.

### Proof of Concept
1. Configure a `pallet_message_queue::Config::MessageProcessor = BridgeHubMessageRouter<XcmpQueue, EthereumInboundQueue>` (the non-Dual router) on a runtime that also has `snowbridge-pallet-inbound-queue-v2` enabled.
2. A relayer submits a valid Ethereum message via `inbound-queue-v2::Call::submit`; the pallet enqueues the resulting payload into `pallet_message_queue` under `AggregateMessageOrigin::SnowbridgeV2(hash)`.
3. On queue processing, `BridgeHubMessageRouter::process_message` is invoked with `origin = SnowbridgeV2(_)`, which hits the `SnowbridgeV2(_) => Err(ProcessMessageError::Unsupported)` arm [4](#0-3) .
4. `pallet_message_queue` marks this message permanently unprocessable; no XCM is ever produced or delivered to AssetHub for this message or any subsequent message on that origin/channel, resulting in a stalled bridge queue that requires a runtime upgrade to fix — an unprivileged relayer can trigger this simply by using the (legitimate, documented) V2 submission flow.

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

**File:** cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs (L103-131)
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
```

**File:** cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs (L134-166)
```rust
/// Routes messages to either the XCMP|Snowbridge V1 processor|Snowbridge V2 processor
pub struct BridgeHubDualMessageRouter<XcmpProcessor, SnowbridgeProcessor, SnowbridgeProcessorV2>(
	PhantomData<(XcmpProcessor, SnowbridgeProcessor, SnowbridgeProcessorV2)>,
)
where
	XcmpProcessor: ProcessMessage<Origin = AggregateMessageOrigin>,
	SnowbridgeProcessor: ProcessMessage<Origin = AggregateMessageOrigin>;

impl<XcmpProcessor, SnowbridgeProcessor, SnowbridgeProcessorV2> ProcessMessage
	for BridgeHubDualMessageRouter<XcmpProcessor, SnowbridgeProcessor, SnowbridgeProcessorV2>
where
	XcmpProcessor: ProcessMessage<Origin = AggregateMessageOrigin>,
	SnowbridgeProcessor: ProcessMessage<Origin = AggregateMessageOrigin>,
	SnowbridgeProcessorV2: ProcessMessage<Origin = AggregateMessageOrigin>,
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
			SnowbridgeV2(_) => SnowbridgeProcessorV2::process_message(message, origin, meter, id),
		}
	}
}
```
