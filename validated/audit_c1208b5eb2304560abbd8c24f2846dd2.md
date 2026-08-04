Based on my research, I found a strong structural analog to the reported bug pattern. I was unable to complete verification of exactly which bridge-hub runtime wires which router in the final iteration, so I flag that explicitly below.

### Title
`BridgeHubMessageRouter` unconditionally rejects `SnowbridgeV2` origin messages as `Unsupported`, permanently stalling Snowbridge V2 inbound processing - (File: cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs)

### Summary
The Sherlock report's root cause is a routing/allowlist function (`TaxTokensReceipt::transferFrom`) that only recognizes a fixed, incomplete set of legitimate destination contracts, so a valid new consumer (Buy Order) is unconditionally rejected and the feature is permanently broken for every user, with no malicious actor involved. The same *shape* of bug exists in the BridgeHub `MessageQueue` origin router: `BridgeHubMessageRouter::process_message` enumerates the `AggregateMessageOrigin` variants and has a dedicated match arm that maps `SnowbridgeV2` to an unconditional `Err(ProcessMessageError::Unsupported)`, in contrast to its sibling `BridgeHubDualMessageRouter` which correctly dispatches `SnowbridgeV2` to a real processor. [1](#0-0) 

### Finding Description
`AggregateMessageOrigin` has five variants, including `SnowbridgeV2(H256)` which is produced whenever the Snowbridge V2 inbound flow enqueues a message into the shared `pallet_message_queue` instance. [2](#0-1) [3](#0-2) 

`BridgeHubMessageRouter::process_message` routes `Here | Parent | Sibling` to the XCMP processor and `Snowbridge(_)` (V1) to the Snowbridge V1 processor, but for `SnowbridgeV2(_)` it does not call any processor at all — it simply returns `Err(ProcessMessageError::Unsupported)`:
```rust
match origin {
    Here | Parent | Sibling(_) => XcmpProcessor::process_message(message, origin, meter, id),
    Snowbridge(_) => SnowbridgeProcessor::process_message(message, origin, meter, id),
    SnowbridgeV2(_) => Err(ProcessMessageError::Unsupported),
}
``` [4](#0-3) 

This is functionally identical to the original bug: an authorization/routing switch that enumerates known legitimate paths but omits a valid one, so any message legitimately using that path is always rejected — not because of malformed input, but because the router itself has no branch that ever succeeds for it.

The codebase itself acknowledges this gap was fixed by introducing a second router, `BridgeHubDualMessageRouter`, whose only functional difference is that it correctly forwards `SnowbridgeV2(_)` to a `SnowbridgeProcessorV2`:
```rust
Snowbridge(_) => SnowbridgeProcessor::process_message(message, origin, meter, id),
SnowbridgeV2(_) => SnowbridgeProcessorV2::process_message(message, origin, meter, id),
``` [5](#0-4) 

The existence of `BridgeHubMessageRouter` as a still-compiled, still-exported type in the same module means any runtime configuration (current or future) that wires `pallet_message_queue::Config::MessageProcessor` to `BridgeHubMessageRouter` instead of `BridgeHubDualMessageRouter`, while simultaneously running an active `snowbridge-pallet-inbound-queue-v2`/`outbound-queue-v2` that enqueues messages under `AggregateMessageOrigin::SnowbridgeV2`, would cause every such message to permanently fail with `Unsupported` — with no relayer, validator, or admin misbehavior needed. This exactly mirrors the external report's "root cause": a hard-coded, incomplete allowlist/dispatch table that silently excludes a legitimate, protocol-native flow.

**Note on verification**: I was not able to conclusively confirm, within the available iterations, whether any currently *live* bridge-hub runtime (`bridge-hub-westend`, `bridge-hub-rococo`) actually wires `pallet_message_queue`'s `MessageProcessor` to the non-dual `BridgeHubMessageRouter` while also having Snowbridge V2 pallets active in the same runtime. Grep results show both `BridgeHubMessageRouter` and `BridgeHubDualMessageRouter` referenced across `bridge-hub-rococo/src/lib.rs` and `bridge-hub-westend/src/lib.rs`, so which is used for V2-enabled deployments needs to be checked directly in those runtime configs before treating this as confirmed live-scope.

### Impact Explanation
If this mis-wiring exists in a live deployment, every Snowbridge V2 inbound message (carrying bridged ETH/ERC20 value and relayer reward claims) enqueued under `SnowbridgeV2` origin would be dropped as `Unsupported` by the shared `MessageQueue` pallet, rather than being retried as `Overweight` — since `Unsupported` is treated by `pallet_message_queue` as a permanent, non-retryable failure. This would produce a permanent stall of bridge processing and cause committed inbound value/relayer rewards tied to those messages to become permanently unprocessable/stuck, matching the "public underpriced work that degrades block production or stalls bridge processing" and "permanent user-fund or bridge-state lock" impact categories.

### Likelihood Explanation
Likelihood is contingent entirely on whether the non-dual `BridgeHubMessageRouter` is actually configured as the `MessageProcessor` in a runtime that also has Snowbridge V2 pallets enqueuing `SnowbridgeV2`-origin messages — this could not be fully confirmed in this pass. If it is misconfigured this way, the trigger condition is simply the normal, unprivileged operation of the Snowbridge V2 inbound flow (any relayer submitting any valid Ethereum message), requiring no attacker action.

### Recommendation
Audit every bridge-hub runtime's `pallet_message_queue::Config::MessageProcessor` to confirm it is bound to `BridgeHubDualMessageRouter` (or an equivalent router with a real `SnowbridgeV2` branch) wherever Snowbridge V2 inbound/outbound pallets are active. Consider deprecating/removing `BridgeHubMessageRouter` entirely (or renaming it to make clear it lacks V2 support) to prevent future accidental reuse in a V2-enabled runtime.

### Proof of Concept
Conceptual PoC (requires confirming the vulnerable wiring first):
1. Deploy/point a bridge-hub runtime's `MessageQueue::MessageProcessor` at `BridgeHubMessageRouter<XcmpProcessor, SnowbridgeProcessor>` while also including `snowbridge-pallet-inbound-queue-v2`.
2. Have any relayer submit a valid Ethereum-origin message via `EthereumInboundQueueV2::process_message`, causing a `SnowbridgeV2(H256)`-origin entry to be enqueued into the shared `MessageQueue`.
3. Observe `pallet_message_queue::Event::Processed { success: false, .. }` with `ProcessMessageError::Unsupported` for every such message, regardless of message validity — permanently blocking that channel's inbound processing.

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

**File:** cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs (L86-90)
```rust
impl From<H256> for AggregateMessageOrigin {
	fn from(hash: H256) -> Self {
		Self::SnowbridgeV2(hash)
	}
}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/common/src/message_queue.rs (L117-131)
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
			SnowbridgeV2(_) => Err(ProcessMessageError::Unsupported),
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
