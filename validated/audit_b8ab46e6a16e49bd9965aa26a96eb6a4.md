Audit Report

## Title
Bridge close/reopen with a reused deterministic `LaneId` resets message nonces, allowing duplicate dispatch of an already-settled XCM message - (File: `bridges/modules/xcm-bridge-hub/src/lib.rs`)

## Summary
`pallet_xcm_bridge_hub::close_bridge` fully purges `InboundLaneData`/`OutboundLaneData` for a lane once its queued messages are pruned, and a subsequent `open_bridge` between the same two locations recreates the lane at the same deterministic `LaneId` with nonce counters reset to defaults [1](#0-0) . Because the messages pallet's sole replay-protection mechanism for inbound dispatch is the per-lane `last_delivered_nonce` counter [2](#0-1) , resetting it to zero on lane recreation allows a previously-delivered and dispatched message (nonce 1) to be resubmitted and dispatched a second time after the bridge is closed and reopened.

## Finding Description
`LaneId` is deterministically derived from the two bridge endpoint locations and is explicitly documented to "never change," since it must remain identical on both sides of the bridge and is embedded in message key proofs [3](#0-2) . When `close_bridge` completes pruning of the outbound message queue, it invokes `inbound_lane.purge()` and `outbound_lane.purge()`, deleting the lane storage entries for that `LaneId` along with the `Bridges`/`LaneToBridge` mappings [1](#0-0) .

`LanesManager::create_inbound_lane`/`create_outbound_lane`, invoked when a bridge is reopened, insert fresh `InboundLaneData`/`OutboundLaneData` with `..Default::default()`, which sets `last_delivered_nonce`/`latest_received_nonce` back to `0` [4](#0-3) [5](#0-4) .

`InboundLane::receive_message`, called from the permissionless `receive_messages_proof` extrinsic, only validates that the incoming nonce equals `last_delivered_nonce + 1` on the *current* lane state — it has no persistent, lane-independent memory of nonces already dispatched during a prior lifetime of the same `LaneId` [2](#0-1) . Consequently, after a close/reopen cycle, nonce `1` (and subsequent nonces) become valid again and a relayer holding the original message payload and a still-valid finality/storage proof can resubmit it, causing the pallet to dispatch the exact same XCM a second time.

## Impact Explanation
The dispatched payload on XCM bridge lanes is typically an XCM originating from an `ExportMessage`, commonly performing asset teleports or reserve transfers that unlock or mint funds on the receiving chain. Re-dispatching an already-settled message enables duplicate execution of a fund-moving XCM, resulting in an unbacked asset unlock/mint on the destination chain — matching the "theft or unbacked mint or unlock" and "duplicate settlement or payout" impact categories, and violating the pivot that message queues/receipts/payout state must advance exactly once and that proofs must bind lane, nonce, and replay domain exactly once.

## Likelihood Explanation
`close_bridge` and `open_bridge` are ordinary extrinsics reachable by any origin authorized under `T::OpenBridgeOrigin` (e.g., a sibling parachain's sovereign/XCM origin), and closing/reopening a bridge between the same two endpoints is a plausible operational lifecycle event, not privileged governance abuse being exploited directly — the actual exploit step, resubmission via `receive_messages_proof`, is executed by an unprivileged relayer with a signed origin and a valid header/storage proof, requiring no special privilege. The relayer only needs to retain the original message bytes and a still-unpruned finality proof, which is realistic given relayers routinely retain historical proofs for reward-claim purposes.

## Recommendation
Do not reset the inbound/outbound nonce counters to zero when reopening a bridge on a previously used `LaneId`. Persist `last_delivered_nonce`/`latest_generated_nonce` (or an incrementing epoch/generation counter bound into the replay domain) across `close_bridge`/`open_bridge` cycles, or mint a fresh, never-reused `LaneId` whenever a bridge is reopened after being fully closed and purged, so that a proof for a message dispatched in a prior bridge lifetime can never be re-accepted.

## Proof of Concept
1. Chain A and Chain B open a bridge; lane `L = calculate_lane_id(A, B)` is created in `Opened` state with default nonces.
2. A fund-unlocking XCM message is sent with nonce 1 and dispatched via `receive_messages_proof`; the relayer retains the message and its finality/storage proof.
3. The authorized origin calls `close_bridge` repeatedly until the outbound queue is fully pruned, triggering `inbound_lane.purge()`/`outbound_lane.purge()` on lane `L` [1](#0-0) .
4. The same or another authorized origin calls `open_bridge` for the same `(A, B)` pair; `create_inbound_lane` recreates lane `L` with `last_delivered_nonce = 0` [6](#0-5) .
5. The relayer resubmits the original nonce-1 proof via `receive_messages_proof`; `InboundLane::receive_message` accepts it since `Some(1) == Some(0+1)` [2](#0-1) , and the same fund-unlocking XCM is dispatched a second time.

### Citations

**File:** bridges/modules/xcm-bridge-hub/src/lib.rs (L408-412)
```rust
			// else we have pruned all messages, so lanes and the bridge itself may gone
			inbound_lane.purge();
			outbound_lane.purge();
			Bridges::<T, I>::remove(locations.bridge_id());
			LaneToBridge::<T, I>::remove(bridge.lane_id);
```

**File:** bridges/modules/messages/src/inbound_lane.rs (L186-194)
```rust
	pub fn receive_message<Dispatch: MessageDispatch<LaneId = S::LaneId>>(
		&mut self,
		relayer_at_bridged_chain: &S::Relayer,
		nonce: MessageNonce,
		message_data: DispatchMessageData<Dispatch::DispatchPayload>,
	) -> ReceptionResult<Dispatch::DispatchLevelResult> {
		let mut data = self.storage.data();
		if Some(nonce) != data.last_delivered_nonce().checked_add(1) {
			return ReceptionResult::InvalidNonce;
```

**File:** bridges/modules/messages/src/lanes_manager.rs (L68-101)
```rust
	/// Create new inbound lane in `Opened` state.
	pub fn create_inbound_lane(
		&self,
		lane_id: T::LaneId,
	) -> Result<InboundLane<RuntimeInboundLaneStorage<T, I>>, LanesManagerError> {
		InboundLanes::<T, I>::try_mutate(lane_id, |lane| match lane {
			Some(_) => Err(LanesManagerError::InboundLaneAlreadyExists),
			None => {
				*lane = Some(StoredInboundLaneData(InboundLaneData {
					state: LaneState::Opened,
					..Default::default()
				}));
				Ok(())
			},
		})?;

		self.active_inbound_lane(lane_id)
	}

	/// Create new outbound lane in `Opened` state.
	pub fn create_outbound_lane(
		&self,
		lane_id: T::LaneId,
	) -> Result<OutboundLane<RuntimeOutboundLaneStorage<T, I>>, LanesManagerError> {
		OutboundLanes::<T, I>::try_mutate(lane_id, |lane| match lane {
			Some(_) => Err(LanesManagerError::OutboundLaneAlreadyExists),
			None => {
				*lane = Some(OutboundLaneData { state: LaneState::Opened, ..Default::default() });
				Ok(())
			},
		})?;

		self.active_outbound_lane(lane_id)
	}
```

**File:** bridges/primitives/messages/src/lib.rs (L478-496)
```rust
impl OutboundLaneData {
	/// Returns default outbound lane data with opened state.
	pub fn opened() -> Self {
		OutboundLaneData { state: LaneState::Opened, ..Default::default() }
	}
}

impl Default for OutboundLaneData {
	fn default() -> Self {
		OutboundLaneData {
			state: LaneState::Closed,
			// it is 1 because we're pruning everything in [oldest_unpruned_nonce;
			// latest_received_nonce]
			oldest_unpruned_nonce: 1,
			latest_received_nonce: 0,
			latest_generated_nonce: 0,
		}
	}
}
```
