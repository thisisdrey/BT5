### Title
Closing and reopening an XCM bridge lane resets message nonces on a deterministic `LaneId`, permanently desynchronizing and stalling the bridge - ([File: bridges/modules/xcm-bridge-hub/src/lib.rs])

### Summary
`pallet-xcm-bridge-hub::close_bridge` fully purges the `InboundLanes`/`OutboundLanes` storage for a bridge's lane, and `open_bridge` recreates them with nonce counters reset to their `Default` (zero) values. Because the `LaneId` is a **pure deterministic hash** of the two bridge endpoint locations (`BridgeLocations::calculate_lane_id`), reopening a bridge between the same two chains always produces the exact same `LaneId` used before closure. If only one side of the bridge closes and reopens (the other side's lane is untouched), the two sides end up with the same lane identity but desynchronized nonce state — mirroring the SKALE `MessageProxy` bug where `removeConnectedChain`/`_addConnectedChain` reset `incomingMessageCounter`/`outgoingMessageCounter` to 0 for a chain hash that is reused on reconnection.

### Finding Description
- `LaneId` is computed purely from the ordered, hashed universal locations of the two bridge endpoints and is explicitly documented to "never change" for a given pair of chains: [1](#0-0) 
This is the same primitive as SKALE's `schainHash = keccak256(schainName)` — a deterministic identifier that survives removal and re-creation of the connection.

- `close_bridge` purges the local inbound/outbound lane storage entirely (removing `InboundLaneData`/`OutboundLaneData`, including nonce counters) once the outbound queue is drained: [2](#0-1) 

- `open_bridge` recomputes the same `lane_id` via `calculate_lane_id` and calls `create_inbound_lane`/`create_outbound_lane`, which only check "does an entry already exist for this lane_id" (blocking re-creation while the old entry is present) but insert brand-new `Default` state — nonces at `0`/`1` — once the old entry has been purged: [3](#0-2) [4](#0-3) 

- There is no cross-chain handshake or notification mechanism that forces the bridged (remote) side to close/reset its own lane state when one side closes and reopens; a search of the pallet for any "notify remote of closure" logic found none. The `open_bridge`/`close_bridge` calls are entirely local, single-sided operations gated only by `T::OpenBridgeOrigin` (e.g. a sibling parachain or the parent relay chain) — normal, permissionless-relative-to-its-own-side bridge management, not privileged bridge-wide governance.

- The messages pallet enforces strict, monotonically increasing nonce delivery per lane ("message with nonce N will be delivered right before nonce N+1"): [5](#0-4) 

Consequently: if chain A closes its side of the bridge to chain B (purging its lanes) and then reopens it (recreating the same `lane_id` with nonces reset to 0), while chain B's lane (for the identical `lane_id`) was never closed and still has `latest_generated_nonce`/`last_delivered_nonce` at some N > 0, the two sides are now permanently out of sync on the exact same `LaneId`. New messages A sends will start again at nonce 1, which B's `InboundLane` will reject because it expects `N+1` next (or vice versa for messages from B to A). There is no code path to resynchronize these counters other than manual, coordinated governance intervention on both chains simultaneously — an operational property the protocol does not enforce or even detect automatically.

### Impact Explanation
This directly hits the required impact categories: "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" and "permanent user-fund or bridge-state lock" / "stalls bridge processing." Once desynchronized:
- Legitimate relayers cannot deliver any further messages on the lane (their nonce continuity checks will permanently fail), stalling all XCM traffic (including asset transfers) over that bridge lane.
- Funds/messages already in flight or queued for the closed-then-reopened side can become permanently stuck, since the bridge cannot be "fixed" without a runtime storage migration or governance-level manual nonce resync on both chains — exactly the kind of unrecoverable, non-governance-triggered lock the SKALE report is about.

### Likelihood Explanation
`open_bridge`/`close_bridge` are the pallet's standard, documented lifecycle calls, gated only by `T::OpenBridgeOrigin` — i.e., a single sibling parachain (in control only of its own side) can legitimately close its lane (e.g., after draining its outbound queue) and later reopen it, without any requirement that the remote side agree to or be aware of this timing. Because `calculate_lane_id` is a pure function of the two endpoints, this is a highly likely operational scenario, not a contrived edge case — any bridge operator that closes and later reopens a lane (e.g., to withdraw its deposit and later resume the bridge) will trigger this desync unless it separately coordinates an out-of-band remote-side close-and-reopen, which the protocol does not enforce.

### Recommendation
Do not allow the nonce counters to reset to default when a lane bearing a previously-used, deterministically-derived `LaneId` is (re)created. Options:
1. When purging a lane in `close_bridge`, persist the last nonce state (e.g., in a separate "retired lane" map) instead of fully removing it, and have `create_inbound_lane`/`create_outbound_lane` restore from that retained state if present for the same `LaneId`, mirroring the SKALE fix of not resetting `incomingMessageCounter`/`outgoingMessageCounter`.
2. Alternatively, make `LaneId` generation include a monotonically increasing "epoch"/nonce component (e.g., incremented on each open) so that a reopened bridge between the same two endpoints always gets a fresh, never-before-used `LaneId`, avoiding any possibility of nonce-state collision with a still-open remote lane.
3. At minimum, require and verify (e.g., via a two-phase close handshake over XCM) that both sides of a bridge have fully closed and purged their lanes before allowing either side to reopen with the same `LaneId`.

### Proof of Concept
1. Parachain A opens a bridge to Parachain B via `pallet_xcm_bridge_hub::open_bridge`; `lane_id = calculate_lane_id(A, B)` is created on both sides with `latest_generated_nonce = 0` / `last_delivered_nonce = 0`.
2. A and B exchange messages normally; after N messages, A's outbound lane has `latest_generated_nonce = N`, and B's inbound lane has `last_delivered_nonce = N` (nonces confirmed/pruned via `close_bridge`'s message-pruning loop is not required for this step).
3. Parachain A calls `close_bridge` for its side. Its `OutboundLanes`/`InboundLanes` entries for `lane_id` are fully purged (`purge()` removing the storage key) — see [2](#0-1) . B's lanes are untouched and still record nonce `N`.
4. Parachain A calls `open_bridge` again for the same destination `B`. `calculate_lane_id` returns the identical `lane_id` as before (deterministic hash) — see [1](#0-0) . `create_outbound_lane`/`create_inbound_lane` succeed (no prior entry exists after the purge) and insert fresh `Default` data with nonce `0` — see [3](#0-2) .
5. A sends a new XCM message; its outbound lane assigns nonce `1`. A relayer submits `receive_messages_proof` to B for nonce `1`. B's inbound lane, still expecting nonce `N+1` (from its unmodified state), rejects the message as out of sequence — the lane is now permanently stalled for both directions until manual, coordinated intervention (storage migration/governance on both chains) resynchronizes the counters.

### Citations

**File:** bridges/primitives/xcm-bridge-hub/src/lib.rs (L328-358)
```rust
	/// Generates the exact same `LaneId` on the both bridge hubs.
	///
	/// Note: Use this **only** when opening a new bridge.
	pub fn calculate_lane_id<LaneId: LaneIdType>(
		&self,
		xcm_version: XcmVersion,
	) -> Result<LaneId, BridgeLocationsError> {
		// a tricky helper struct that adds required `Ord` support for
		// `VersionedInteriorLocation`
		#[derive(Eq, PartialEq, Ord, PartialOrd)]
		struct EncodedVersionedInteriorLocation(sp_std::vec::Vec<u8>);
		impl Encode for EncodedVersionedInteriorLocation {
			fn encode(&self) -> sp_std::vec::Vec<u8> {
				self.0.clone()
			}
		}

		let universal_location1 =
			VersionedInteriorLocation::from(self.bridge_origin_universal_location.clone())
				.into_version(xcm_version)
				.map_err(|_| BridgeLocationsError::UnsupportedXcmVersion);
		let universal_location2 =
			VersionedInteriorLocation::from(self.bridge_destination_universal_location.clone())
				.into_version(xcm_version)
				.map_err(|_| BridgeLocationsError::UnsupportedXcmVersion);

		LaneId::try_new(
			EncodedVersionedInteriorLocation(universal_location1.encode()),
			EncodedVersionedInteriorLocation(universal_location2.encode()),
		)
		.map_err(|_| BridgeLocationsError::UnsupportedLaneIdType)
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

**File:** bridges/modules/messages/README.md (L21-25)
```markdown
Messages module supports multiple message lanes. Every message lane is identified with a 4-byte identifier. Messages
sent through the lane are assigned unique (for this lane) increasing integer value that is known as nonce ("number that
can only be used once"). Messages that are sent over the same lane are guaranteed to be delivered to the target chain in
the same order they're sent from the source chain. In other words, message with nonce `N` will be delivered right before
delivering a message with nonce `N+1`.
```
