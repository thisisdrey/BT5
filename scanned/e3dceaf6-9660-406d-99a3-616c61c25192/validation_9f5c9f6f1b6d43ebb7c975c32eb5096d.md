### Title
Inbound bridge message nonce is committed as "delivered" even when XCM dispatch fails, permanently barring retry of the underlying value transfer - (File: `bridges/modules/messages/src/inbound_lane.rs`)

### Summary
The Boba report's core broken invariant is: a monotonically-advancing settlement counter (`depositId`) is bumped even when the actual value-moving operation fails, and once bumped there is no way to resubmit that exact item because the nonce-equality check now rejects it. The Polkadot SDK bridge-messages pallet (`pallet-bridge-messages`) has the same structural defect: `InboundLane::receive_message` advances `last_delivered_nonce` unconditionally, regardless of whether `MessageDispatch::dispatch` actually succeeded.

### Finding Description
In `bridges/modules/messages/src/inbound_lane.rs`, `receive_message` first validates strict nonce sequencing: [1](#0-0) 
then calls `Dispatch::dispatch(...)` and, **regardless of the result**, commits the delivered-nonce state (`data.relayers` / `last_delivered_nonce`) via `self.storage.set_data(data)`: [2](#0-1) 

The primitive type documents this intentionally: "Message has been received and dispatched. Note that we don't care whether dispatch has been successful or not - in both case message falls into this category": [3](#0-2) 

The caller, `pallet_bridge_messages::Pallet::receive_messages_proof`, explicitly treats dispatch failure the same as dispatch success for weight/refund accounting and never rolls back the nonce advance: [4](#0-3) 

The concrete `MessageDispatch` implementation used for XCM-over-bridge (`pallet-xcm-bridge-hub`) forwards the payload to `T::BlobDispatcher::dispatch_blob`, and on failure just returns `XcmBlobMessageDispatchResult::NotDispatched(Some(e))` — the message is *not* re-queued, it is simply dropped after being marked delivered: [5](#0-4) 

`MessageDispatch::is_active` is documented as only a *hint*: "the consumer may still call the `dispatch` if dispatcher has returned `false`": [6](#0-5) 

So a transient condition on the destination side (sibling HRMP channel not yet open, congestion, routing error — the exact equivalent of the "recipient temporarily paused" scenario in the Boba report) causes `DispatchBlobError`/`NotDispatched`, yet the nonce for that message is permanently consumed. Because `receive_message` rejects any nonce that isn't exactly `last_delivered_nonce + 1`, the relayer's off-chain retry mechanism (resubmitting the same message/proof) can never succeed for that nonce — structurally identical to Boba's `_depositId == totalDisbursements[_sourceChainId]` check failing after `totalDisbursements` has already been bumped.

Unlike the Snowbridge V2 inbound-queue path (which, when the *inner* XCM program traps assets during execution, benefits from `pallet_xcm`'s `AssetsTrapped`/`ClaimAsset` recovery mechanism), the bridge-messages/xcm-bridge-hub dispatch path has no such recovery: if `dispatch_blob` itself fails (routing/decoding/congestion), the XCM program never even reaches an executor that could trap assets — the payload is discarded outright with only a trace-level log, and no on-chain event records the loss.

### Impact Explanation
Any XCM payload relayed through a bridge lane that fails at the `dispatch_blob` stage (not XCM-VM execution, but the earlier blob-routing stage) is permanently lost with no retry path and no claimable trap. If that payload represented a reserve-asset/teleport instruction whose corresponding value was already locked/burned on the source chain when the outbound message was enqueued, the destination-side mint/deposit never happens — a straightforward value loss with no admin/relayer misbehavior required, satisfying the "permanent user-fund or bridge-state lock" / "theft or unbacked mint or unlock" impact class.

### Likelihood Explanation
Triggering this requires only an ordinary, permissionless relayer submitting a valid message proof while the destination routing conditions (e.g., HRMP channel state) are transiently unfavorable — no malicious relayer, validator, or governance action is needed. `is_active` is explicitly documented as best-effort, so this is a reachable condition in normal operation, not solely a contrived edge case. Precise triggering (getting a `NotDispatched` result specifically, rather than the message simply queueing weight-wise) requires the destination-side channel/routing state to be unfavorable at the exact block the messages-proof extrinsic executes, so likelihood is real but not "trivial to reproduce on demand" without also controlling the destination-chain HRMP/queue state.

### Recommendation
- Do not treat `ReceptionResult::Dispatched` as a terminal delivered state when the inner `dispatch_level_result` indicates non-dispatch (e.g., `XcmBlobMessageDispatchResult::NotDispatched`); either retry within the same block/weight budget, or expose a permissioned/permissionless re-dispatch entrypoint keyed by nonce (mirroring the Boba fix of adding a retry function) so failed-but-delivered nonces are not silently unrecoverable.
- Emit a distinct on-chain event (not just a trace log) whenever `dispatch_blob`/`dispatch` returns a failure variant, so relayers/users can detect and act on lost messages, matching the report's recommendation to "emit an event to indicate a failure."
- Consider decoupling "nonce delivered" (replay protection) from "payload successfully routed," e.g., by storing failed dispatch outcomes in a queryable map (similar to `pallet_xcm`'s `AssetsTrapped`) that supports a claim/retry call, rather than only relying on best-effort `is_active` hints before submission.

### Proof of Concept
1. Open a bridge lane between Bridge Hub A and Bridge Hub B, with a further HRMP channel from Bridge Hub B to sibling Parachain C not yet opened (or momentarily closed).
2. On chain A, execute a reserve/teleport transfer whose XCM payload is destined for Parachain C via Bridge Hub B (value is withdrawn/locked on A when the outbound message is enqueued).
3. A relayer submits `receive_messages_proof` on Bridge Hub B with the correct nonce and valid proof.
4. `InboundLane::receive_message` dispatches via `pallet_xcm_bridge_hub`'s `MessageDispatch::dispatch`, which calls `dispatch_blob`; because the HRMP channel to C is not open, `DispatchBlobError::RoutingError` is returned and `XcmBlobMessageDispatchResult::NotDispatched(...)` is produced — see the existing unit test demonstrating exactly this outcome: [7](#0-6) 
5. Despite the failed dispatch, `last_delivered_nonce` on Bridge Hub B's inbound lane has already advanced to this nonce (per `receive_message`'s unconditional `self.storage.set_data(data)`).
6. The relayer (or off-chain retry service) attempts to resubmit the identical message/proof to retry delivery once the HRMP channel opens; `receive_message`'s nonce check (`Some(nonce) != data.last_delivered_nonce().checked_add(1)`) now rejects it as `InvalidNonce`, and the value locked on chain A in step 2 is never delivered to Parachain C — permanently stuck, with no on-chain record beyond a trace log.

### Citations

**File:** bridges/modules/messages/src/inbound_lane.rs (L192-195)
```rust
		let mut data = self.storage.data();
		if Some(nonce) != data.last_delivered_nonce().checked_add(1) {
			return ReceptionResult::InvalidNonce;
		}
```

**File:** bridges/modules/messages/src/inbound_lane.rs (L208-229)
```rust
		// then, dispatch message
		let dispatch_result = Dispatch::dispatch(DispatchMessage {
			key: MessageKey { lane_id: self.storage.id(), nonce },
			data: message_data,
		});

		// now let's update inbound lane storage
		match data.relayers.back_mut() {
			Some(entry) if entry.relayer == *relayer_at_bridged_chain => {
				entry.messages.note_dispatched_message();
			},
			_ => {
				data.relayers.push_back(UnrewardedRelayer {
					relayer: relayer_at_bridged_chain.clone(),
					messages: DeliveredMessages::new(nonce),
				});
			},
		};
		self.storage.set_data(data);

		ReceptionResult::Dispatched(dispatch_result)
	}
```

**File:** bridges/primitives/messages/src/lib.rs (L364-378)
```rust
/// Result of single message receival.
#[derive(Debug, Encode, Decode, DecodeWithMemTracking, PartialEq, Eq, Clone, TypeInfo)]
pub enum ReceptionResult<DispatchLevelResult> {
	/// Message has been received and dispatched. Note that we don't care whether dispatch has
	/// been successful or not - in both case message falls into this category.
	///
	/// The message dispatch result is also returned.
	Dispatched(MessageDispatchResult<DispatchLevelResult>),
	/// Message has invalid nonce and lane has rejected to accept this message.
	InvalidNonce,
	/// There are too many unrewarded relayer entries at the lane.
	TooManyUnrewardedRelayers,
	/// There are too many unconfirmed messages at the lane.
	TooManyUnconfirmedMessages,
}
```

**File:** bridges/modules/messages/src/lib.rs (L306-327)
```rust
				let receival_result = lane.receive_message::<T::MessageDispatch>(
					&relayer_id_at_bridged_chain,
					message.key.nonce,
					message.data,
				);

				// note that we're returning unspent weight to relayer even if message has been
				// rejected by the lane. This allows relayers to submit spam transactions with
				// e.g. the same set of already delivered messages over and over again, without
				// losing funds for messages dispatch. But keep in mind that relayer pays base
				// delivery transaction cost anyway. And base cost covers everything except
				// dispatch, so we have a balance here.
				let unspent_weight = match &receival_result {
					ReceptionResult::Dispatched(dispatch_result) => {
						valid_messages += 1;
						dispatch_result.unspent_weight
					},
					ReceptionResult::InvalidNonce |
					ReceptionResult::TooManyUnrewardedRelayers |
					ReceptionResult::TooManyUnconfirmedMessages => message_dispatch_weight,
				};
				messages_received_status.push(message.key.nonce, receival_result);
```

**File:** bridges/modules/xcm-bridge-hub/src/dispatcher.rs (L88-129)
```rust
	fn dispatch(
		message: DispatchMessage<Self::DispatchPayload, Self::LaneId>,
	) -> MessageDispatchResult<Self::DispatchLevelResult> {
		let payload = match message.data.payload {
			Ok(payload) => payload,
			Err(e) => {
				tracing::error!(
					target: LOG_TARGET,
					error=?e,
					lane_id=?message.key.lane_id,
					message_nonce=?message.key.nonce,
					"dispatch - payload error"
				);
				return MessageDispatchResult {
					unspent_weight: Weight::zero(),
					dispatch_level_result: XcmBlobMessageDispatchResult::InvalidPayload,
				};
			},
		};
		let dispatch_level_result = match T::BlobDispatcher::dispatch_blob(payload) {
			Ok(_) => {
				tracing::debug!(
					target: LOG_TARGET,
					lane_id=?message.key.lane_id,
					message_nonce=?message.key.nonce,
					"dispatch - `DispatchBlob::dispatch_blob` was ok"
				);
				XcmBlobMessageDispatchResult::Dispatched
			},
			Err(e) => {
				tracing::error!(
					target: LOG_TARGET,
					error=?e,
					lane_id=?message.key.lane_id,
					message_nonce=?message.key.nonce,
					"dispatch - `DispatchBlob::dispatch_blob` failed"
				);
				XcmBlobMessageDispatchResult::NotDispatched(Some(e))
			},
		};
		MessageDispatchResult { unspent_weight: Weight::zero(), dispatch_level_result }
	}
```

**File:** bridges/primitives/messages/src/target_chain.rs (L101-108)
```rust
	/// Returns `true` if dispatcher is ready to accept additional messages. The `false` should
	/// be treated as a hint by both dispatcher and its consumers - i.e. dispatcher shall not
	/// simply drop messages if it returns `false`. The consumer may still call the `dispatch`
	/// if dispatcher has returned `false`.
	///
	/// We check it in the messages delivery transaction prologue. So if it becomes `false`
	/// after some portion of messages is already dispatched, it doesn't fail the whole transaction.
	fn is_active(lane: Self::LaneId) -> bool;
```

**File:** cumulus/parachains/runtimes/bridge-hubs/test-utils/src/test_cases/mod.rs (L524-538)
```rust
		// 2.1. WITHOUT opened hrmp channel -> RoutingError
		let result =
			<<Runtime as BridgeMessagesConfig<MessagesPalletInstance>>::MessageDispatch>::dispatch(
				DispatchMessage {
					key: MessageKey { lane_id: dummy_lane_id, nonce: 1 },
					data: DispatchMessageData { payload: Ok(bridging_message.clone()) },
				},
			);
		assert_eq!(
			format!("{:?}", result.dispatch_level_result),
			format!(
				"{:?}",
				XcmBlobMessageDispatchResult::NotDispatched(Some(DispatchBlobError::RoutingError))
			)
		);
```
