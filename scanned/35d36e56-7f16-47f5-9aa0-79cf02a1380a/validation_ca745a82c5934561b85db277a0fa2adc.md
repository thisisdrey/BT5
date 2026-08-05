## Analog Found: Bridge closure + reopening resets inbound/outbound message nonces, enabling replay of an already‑dispatched cross‑chain message

### Title
Bridge close/reopen with a reused deterministic `LaneId` resets message nonces, allowing duplicate dispatch of an already-settled XCM message - (File: `bridges/modules/xcm-bridge-hub/src/lib.rs`)

### Summary
`pallet_xcm_bridge_hub::close_bridge` fully purges the inbound/outbound lane storage (`InboundLaneData`/`OutboundLaneData`) once all queued messages are pruned, and removes the `Bridges`/`LaneToBridge` entries [1](#0-0) . `LaneId` is deterministically derived from the two endpoint locations and is documented to "never change" [2](#0-1) , so a subsequent `open_bridge` between the same two locations recreates the lanes at the *same* `LaneId` with brand-new default nonce state (`last_delivered_nonce = 0`, `oldest_unpruned_nonce = 1`, etc.) via `LanesManager::create_inbound_lane`/`create_outbound_lane` [3](#0-2) . This is structurally identical to `Organizer.offboard()` deleting `orgs[msg.sender]` (including `packedPayoutNonces[]`) and a subsequent `onboard()` resetting replay-protection state for the same identity.

### Finding Description
The messages pallet's only replay protection for inbound message dispatch is the `InboundLaneData.last_delivered_nonce()` counter stored per-lane: `receive_message` rejects any nonce that is not exactly `last_delivered_nonce + 1` [4](#0-3) . There is no independent, permanent "used nonce" ledger outside of this per-lane counter.

When `close_bridge` finishes pruning the outbound queue, it calls `inbound_lane.purge()` and `outbound_lane.purge()`, deleting the `InboundLanes`/`OutboundLanes` storage entries for that `LaneId` entirely [1](#0-0) . Because `LaneId` is a pure function of the two bridge endpoint locations (and must stay the same for cryptographic/proof compatibility, per the pallet's own doc comment), reopening the bridge between the identical two locations via `open_bridge`/`do_open_bridge` recreates lanes under the exact same `LaneId`, with `InboundLaneData::default()` (`last_delivered_nonce = 0`) [5](#0-4) .

Once the lane is live again, the nonce sequence 1, 2, 3, … is available again for delivery. `receive_messages_proof` (the messages pallet's permissionless, signed-origin extrinsic used by relayers) only checks that the supplied storage/finality proof for the bridged chain's header is valid and that the message's nonce matches `last_delivered_nonce + 1` on this (now-reset) lane — it has no memory that nonce `1` (etc.) at this `LaneId` was already dispatched during the bridge's previous lifetime. A relayer who retained (or can reconstruct) the storage proof of a message that was already delivered and dispatched *before* the bridge was closed can resubmit it after the bridge reopens; the pallet will accept it as "nonce 1 of the new lane" and dispatch it a second time.

### Impact Explanation
The dispatched payload for XCM bridge lanes is typically a `ExportMessage`-originated XCM that performs asset teleports/reserve-transfers, unlocking or minting funds on the receiving chain (this is exactly the Snowbridge/XCM-bridge-hub delivery flow referenced in the Impact Gate: "duplicate settlement or payout... theft or unbacked mint or unlock"). Re-dispatching an already-settled message allows an unprivileged relayer to cause the destination chain to execute the same fund-moving XCM twice, resulting in duplicate/unbacked asset unlock or mint — the direct analog of "drain a safe" in the original report. Value is not conserved and is not settled exactly once, violating the stated pivot: "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" and must bind "nonce... and replay domain exactly once."

### Likelihood Explanation
`close_bridge`/`open_bridge` are ordinary, non-privileged pallet extrinsics reachable by any XCM origin permitted by `T::OpenBridgeOrigin` (a sibling parachain or the relay chain), and closing+reopening a bridge between the same two locations is an operationally plausible/expected lifecycle event (e.g. bridge maintenance, migration, fee reconfiguration) rather than an exotic edge case. `receive_messages_proof` itself is callable by any relayer with a signed account and a valid header/storage proof — no special privilege is required to exploit the reset once it occurs. The relayer only needs to retain the message bytes and a still-valid (unpruned) finality/header proof from the bridge's earlier life, which is realistic given bridge relayers routinely retain historical proofs/messages for reward-claim purposes.

### Recommendation
Do not fully reset the inbound/outbound nonce counters when reopening a bridge on a `LaneId` that was previously used. Options mirroring the original report's suggestions:
- Persist `last_delivered_nonce`/`latest_generated_nonce` (or a monotonic "epoch" counter) across `close_bridge`/`open_bridge` cycles instead of deleting them, so nonces are never reused for the same `LaneId`.
- Introduce a bridge "epoch" or "generation" number that is included in the nonce/replay domain, incremented on every `open_bridge`, so an old proof anchored to a stale epoch is rejected even if the raw nonce value coincides.
- Alternatively, mint a fresh, never-before-used `LaneId` (rather than the deterministic, endpoint-derived one) whenever a bridge is reopened after being fully closed and purged.

### Proof of Concept
1. Chain A and Chain B open a bridge; lane `L = calculate_lane_id(A, B)` is created in `Opened` state with nonces at defaults.
2. A message (e.g., XCM teleport unlocking 1000 UNIT on Chain B) is sent with nonce 1, delivered via `receive_messages_proof`, and dispatched — funds unlocked once. The relayer keeps the original message payload and the finality/storage proof used (or the header remains within the still-tracked/unpruned finality window).
3. Governance/parachain owner calls `close_bridge` repeatedly until `may_prune_messages` drains the outbound queue; `inbound_lane.purge()`/`outbound_lane.purge()` run, deleting `InboundLanes::<T,I>::get(L)`/`OutboundLanes::<T,I>::get(L)` [1](#0-0) .
4. The same parachain (or any party controlling the same `bridge_origin_universal_location`) calls `open_bridge` again for the same `(A, B)` pair. `do_open_bridge` recreates lane `L` via `create_inbound_lane`, resetting `last_delivered_nonce` to 0 [6](#0-5) .
5. The relayer resubmits the original nonce-1 proof/message from step 2 via `receive_messages_proof`. `InboundLane::receive_message` sees `Some(1) == data.last_delivered_nonce().checked_add(1)` (now `Some(0+1)`), the check passes, and the exact same XCM (unlocking another 1000 UNIT) is dispatched again [7](#0-6)  — a duplicate settlement of an already-paid-out message.

### Citations

**File:** bridges/modules/xcm-bridge-hub/src/lib.rs (L39-43)
```rust
//! `LaneId` is expected to never change because:
//! - We need the same `LaneId` on both sides of the bridge, as `LaneId` is part of the message key
//!   proofs.
//! - Runtime upgrades are entirely asynchronous.
//! - We already have a running production Polkadot/Kusama bridge that uses `LaneId([0, 0, 0, 0])`.
```

**File:** bridges/modules/xcm-bridge-hub/src/lib.rs (L408-412)
```rust
			// else we have pruned all messages, so lanes and the bridge itself may gone
			inbound_lane.purge();
			outbound_lane.purge();
			Bridges::<T, I>::remove(locations.bridge_id());
			LaneToBridge::<T, I>::remove(bridge.lane_id);
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

**File:** bridges/modules/messages/src/inbound_lane.rs (L186-212)
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
		}

		// if there are more unrewarded relayer entries than we may accept, reject this message
		if data.relayers.len() as MessageNonce >= self.storage.max_unrewarded_relayer_entries() {
			return ReceptionResult::TooManyUnrewardedRelayers;
		}

		// if there are more unconfirmed messages than we may accept, reject this message
		let unconfirmed_messages_count = nonce.saturating_sub(data.last_confirmed_nonce);
		if unconfirmed_messages_count > self.storage.max_unconfirmed_messages() {
			return ReceptionResult::TooManyUnconfirmedMessages;
		}

		// then, dispatch message
		let dispatch_result = Dispatch::dispatch(DispatchMessage {
			key: MessageKey { lane_id: self.storage.id(), nonce },
			data: message_data,
		});
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
