## Title
Shared bridge-lane congestion threshold lets a single unprivileged sender suspend the whole AH↔AH bridge for everyone - (File: `bridges/modules/xcm-bridge-hub/src/exporter.rs`)

## Summary
The Axelar bug reduces to: a global, shared rate/capacity limit that resets per epoch can be exhausted by one large actor at negligible marginal cost, causing the shared resource (the transfer lane) to become unusable for every other legitimate user until the next window. The Polkadot SDK local analog is the `pallet-xcm-bridge-hub` outbound lane congestion mechanism, where a single, chain-wide `OUTBOUND_LANE_CONGESTED_THRESHOLD` (a "capacity" shared by all senders using that lane) triggers a bridge-wide suspend signal once crossed - blocking every other user of the bridge, not just the one who filled the queue.

## Finding Description
`pallet-xcm-bridge-hub`'s exporter enqueues every outbound XCM-over-bridge message into a single lane's outbound queue via `MessagesPallet::<T, I>::send_message`, and then checks the total number of currently enqueued (undelivered) messages against a hard-coded, non-per-account threshold: [1](#0-0) 

```
const OUTBOUND_LANE_CONGESTED_THRESHOLD: MessageNonce = 8_192;
const OUTBOUND_LANE_UNCONGESTED_THRESHOLD: MessageNonce = 1_024;
```

`deliver()` calls `on_bridge_message_enqueued`, which suspends the *entire bridge* (not just the sender) once `enqueued_messages > OUTBOUND_LANE_CONGESTED_THRESHOLD`: [2](#0-1) 

The suspension is communicated via `LocalXcmChannelManager::suspend_bridge`, which for the AssetHub↔AssetHub bridge configuration sends an XCM congestion signal into `XcmpQueue` for the *local origin* location: [3](#0-2) 

The pallet's own doc comment states the design assumption explicitly: **"all outbound messages through this router are using single lane"**: [4](#0-3) 

The only counter-pressure is a *fee* increase implemented in `pallet-xcm-bridge-hub-router`, which raises `delivery_fee_factor` exponentially only in reaction to congestion feedback that has already occurred (i.e., after the queue is already backed up), and decays it slowly once per block in `on_initialize`: [5](#0-4) [6](#0-5) 

This is functionally the same broken invariant as the Axelar `TokenManager` flow limit: a **single global capacity counter, shared by all unprivileged senders, with no per-account/per-flow accounting**, whose threshold can be reached unilaterally by one actor. Just as the Axelar whale could burn the whole epoch's flow-limit budget for everyone, an actor who is a legitimate (non-privileged, non-validator, non-relayer) user of the sending parachain can push 8,193 messages into the shared outbound lane queue before delivery/pruning catches up, tripping the suspend logic and blocking every other user's cross-chain messages on that lane until the queue drains back down to 1,024 (`OUTBOUND_LANE_UNCONGESTED_THRESHOLD`) - a window controlled by relayer throughput, not by the victims.

Unlike the fee-factor mitigation (which only makes *sending* progressively more expensive), the **suspend/resume signal is a hard availability cut**: once suspended, `LocalXcmChannelManager::suspend_bridge` asks the sibling `XcmpQueue` to stop forwarding messages destined for that bridge, which affects all senders on that parachain who use the same bridge/lane — exactly analogous to the ITS `TokenManager` flow limit blocking *all* transfers for the token once the epoch cap is hit, regardless of who is trying to transfer.

## Impact Explanation
This maps onto the "public underpriced work that degrades block production or stalls bridge processing" and "message queues ... must only advance after ... settlement succeed atomically" pivots: an unprivileged party can stall the shared AssetHub↔AssetHub (or any bridge-hub-to-bridge-hub) XCM bridging lane for all other users, purely by being a heavy but otherwise legitimate sender, without needing a malicious relayer, validator, or governance actor. This is a genuine availability/liveness impact on bridge processing for the entire lane's user population, not just the attacker's own messages — mirroring the Medium severity assigned to the original Axelar finding (temporary availability impact on the token service).

## Likelihood Explanation
Likelihood is bounded by the cost of enqueuing thousands of small XCM messages and by the exponential fee-factor counter-measure, so this is not free, and in practice the fee factor and relayer throughput would raise the cost over time. This makes it comparable to (not stronger than) the original Axelar finding, which was itself judged Medium/QA precisely because the rate-limit/threshold behavior is by design and mitigated by operational parameters (here: `OUTBOUND_LANE_UNCONGESTED_THRESHOLD` for auto-resume, and the exponential fee factor). The threshold values (8,192 / 1,024) are fixed constants, not configurable per-deployment, so operators cannot "raise the limit" the way the Axelar team says token-manager operators can raise `flowLimit`.

## Recommendation
- Track and rate-limit outbound message enqueue counts *per sending origin* (e.g. per sibling `ParaId`/location) in addition to the shared lane-wide congestion threshold, so a single origin cannot unilaterally trip the whole-lane suspend.
- Consider making `OUTBOUND_LANE_CONGESTED_THRESHOLD` proportional to fee-factor state, so that suspension is deferred while the fee is already discouraging the flooding sender, rather than being a simple queue-depth trip-wire independent of who is filling the queue.

## Proof of Concept
1. Attacker controls an ordinary (non-privileged) account on a parachain that routes XCM through `pallet-xcm-bridge-hub-router` to a bridge hub running `pallet-xcm-bridge-hub`.
2. Attacker repeatedly calls any XCM-sending extrinsic/route that results in `ExportXcm::deliver` on the single configured lane (`bridges/modules/xcm-bridge-hub/src/exporter.rs:188-206`), e.g. small remote-execution XCMs, paying only the current (still-low) `delivery_fee_factor` computed by `pallet-xcm-bridge-hub-router`.
3. Once the number of enqueued-but-undelivered messages on that lane exceeds `OUTBOUND_LANE_CONGESTED_THRESHOLD = 8_192` (`bridges/modules/xcm-bridge-hub/src/exporter.rs:42`), `on_bridge_message_enqueued` fires and calls `LocalXcmChannelManager::suspend_bridge` (`bridges/modules/xcm-bridge-hub/src/exporter.rs:254-255`), sending a congestion signal into the sibling `XcmpQueue`.
4. All other users of the same bridge/lane on that parachain are now blocked from having their messages delivered/forwarded through the bridge until relayers drain the queue back to `OUTBOUND_LANE_UNCONGESTED_THRESHOLD = 1_024` (`bridges/modules/xcm-bridge-hub/src/exporter.rs:46`, `285-351`), a duration entirely controlled by relayer capacity, not by the victims.
5. This is directly analogous to the Axelar PoC: one actor unilaterally exhausts a shared, non-per-account capacity limit, denying service to everyone else sharing that same channel.

Note: exact economic cost to reach 8,192 enqueued messages (i.e., how quickly `delivery_fee_factor` growth in `xcm-bridge-hub-router` outpaces the attacker) was not fully modeled in this pass and would benefit from a benchmarked simulation in a Devin session with access to the runtime to confirm the attack's real-world cost/likelihood.

### Citations

**File:** bridges/modules/xcm-bridge-hub/src/exporter.rs (L40-46)
```rust
/// Maximal number of messages in the outbound bridge queue. Once we reach this limit, we
/// suspend a bridge.
const OUTBOUND_LANE_CONGESTED_THRESHOLD: MessageNonce = 8_192;

/// After we have suspended the bridge, we wait until number of messages in the outbound bridge
/// queue drops to this count, before sending resuming the bridge.
const OUTBOUND_LANE_UNCONGESTED_THRESHOLD: MessageNonce = 1_024;
```

**File:** bridges/modules/xcm-bridge-hub/src/exporter.rs (L185-206)
```rust
		Ok(((*locations.bridge_id(), bridge, bridge_message, id), price))
	}

	fn deliver(
		(bridge_id, bridge, bridge_message, id): Self::Ticket,
	) -> Result<XcmHash, SendError> {
		let artifacts = MessagesPallet::<T, I>::send_message(bridge_message);

		tracing::info!(
			target: LOG_TARGET,
			topic_id=?id,
			bridge_id=?bridge_id,
			lane_id=?bridge.lane_id,
			nonce=%artifacts.nonce,
			"XCM message has been enqueued"
		);

		// maybe we need switch to congested state
		Self::on_bridge_message_enqueued(bridge_id, bridge, artifacts.enqueued_messages);

		Ok(id)
	}
```

**File:** bridges/modules/xcm-bridge-hub/src/exporter.rs (L215-282)
```rust
impl<T: Config<I>, I: 'static> Pallet<T, I> {
	/// Called when new message is pushed onto outbound bridge queue.
	fn on_bridge_message_enqueued(
		bridge_id: BridgeId,
		bridge: BridgeOf<T, I>,
		enqueued_messages: MessageNonce,
	) {
		// if the bridge queue is not congested, we don't want to do anything
		let is_congested = enqueued_messages > OUTBOUND_LANE_CONGESTED_THRESHOLD;
		if !is_congested {
			return;
		}

		// TODO: https://github.com/paritytech/parity-bridges-common/issues/2006 we either need fishermens
		// to watch this rule violation (suspended, but keep sending new messages), or we need a
		// hard limit for that like other XCM queues have

		// check if the lane is already suspended. If it is, do nothing. We still accept new
		// messages to the suspended bridge, hoping that it'll be actually resumed soon
		if bridge.state == BridgeState::Suspended {
			return;
		}

		// else - suspend the bridge
		let result_bridge_origin_relative_location =
			(*bridge.bridge_origin_relative_location).clone().try_into();
		let bridge_origin_relative_location = match &result_bridge_origin_relative_location {
			Ok(bridge_origin_relative_location) => bridge_origin_relative_location,
			Err(_) => {
				tracing::debug!(
					target: LOG_TARGET,
					?bridge_id,
					origin_location=?bridge.bridge_origin_relative_location,
					"Failed to convert"
				);

				return;
			},
		};
		let suspend_result =
			T::LocalXcmChannelManager::suspend_bridge(bridge_origin_relative_location, bridge_id);
		match suspend_result {
			Ok(_) => {
				tracing::debug!(
					target: LOG_TARGET,
					?bridge_id,
					originated_by=?bridge.bridge_origin_relative_location,
					"Suspended"
				);
			},
			Err(e) => {
				tracing::debug!(
					target: LOG_TARGET,
					error=?e,
					?bridge_id,
					originated_by=?bridge.bridge_origin_relative_location,
					"Failed to suspended"
				);

				return;
			},
		}

		// and remember that we have suspended the bridge
		Bridges::<T, I>::mutate_extant(bridge_id, |bridge| {
			bridge.state = BridgeState::Suspended;
		});
	}
```

**File:** cumulus/parachains/runtimes/bridge-hubs/bridge-hub-rococo/src/bridge_to_westend_config.rs (L163-197)
```rust
/// Implementation of `bp_xcm_bridge_hub::LocalXcmChannelManager` for congestion management.
pub struct CongestionManager;
impl pallet_xcm_bridge_hub::LocalXcmChannelManager for CongestionManager {
	type Error = SendError;

	fn is_congested(with: &Location) -> bool {
		// This is used to check the inbound bridge queue/messages to determine if they can be
		// dispatched and sent to the sibling parachain. Therefore, checking outbound `XcmpQueue`
		// is sufficient here.
		use bp_xcm_bridge_hub_router::XcmChannelStatusProvider;
		cumulus_pallet_xcmp_queue::bridging::OutXcmpChannelStatusProvider::<Runtime>::is_congested(
			with,
		)
	}

	fn suspend_bridge(local_origin: &Location, bridge: BridgeId) -> Result<(), Self::Error> {
		// This bridge is intended for AH<>AH communication with a hard-coded/static lane,
		// so `local_origin` is expected to represent only the local AH.
		send_xcm::<XcmpQueue>(
			local_origin.clone(),
			bp_asset_hub_rococo::build_congestion_message(bridge.inner(), true).into(),
		)
		.map(|_| ())
	}

	fn resume_bridge(local_origin: &Location, bridge: BridgeId) -> Result<(), Self::Error> {
		// This bridge is intended for AH<>AH communication with a hard-coded/static lane,
		// so `local_origin` is expected to represent only the local AH.
		send_xcm::<XcmpQueue>(
			local_origin.clone(),
			bp_asset_hub_rococo::build_congestion_message(bridge.inner(), false).into(),
		)
		.map(|_| ())
	}
}
```

**File:** bridges/modules/xcm-bridge-hub-router/src/lib.rs (L124-157)
```rust
	#[pallet::hooks]
	impl<T: Config<I>, I: 'static> Hooks<BlockNumberFor<T>> for Pallet<T, I> {
		fn on_initialize(_n: BlockNumberFor<T>) -> Weight {
			// if XCM channel is still congested, we don't change anything
			if T::LocalXcmChannelManager::is_congested(&T::SiblingBridgeHubLocation::get()) {
				return T::WeightInfo::on_initialize_when_congested();
			}

			// if bridge has reported congestion, we don't change anything
			let mut bridge = Self::bridge();
			if bridge.is_congested {
				return T::WeightInfo::on_initialize_when_congested();
			}

			let previous_factor = Self::get_fee_factor(());
			// if we can't decrease the delivery fee factor anymore, we don't change anything
			if !Self::do_decrease_fee_factor(&mut bridge.delivery_fee_factor) {
				return T::WeightInfo::on_initialize_when_congested();
			}

			tracing::info!(
				target: LOG_TARGET,
				from=%previous_factor,
				to=%bridge.delivery_fee_factor,
				"Bridge channel is uncongested. Decreased fee factor"
			);
			Self::deposit_event(Event::DeliveryFeeFactorDecreased {
				new_value: bridge.delivery_fee_factor,
			});

			Bridge::<T, I>::put(bridge);

			T::WeightInfo::on_initialize_when_non_congested()
		}
```

**File:** bridges/modules/xcm-bridge-hub-router/src/lib.rs (L204-239)
```rust
		/// Called when new message is sent (queued to local outbound XCM queue) over the bridge.
		pub(crate) fn on_message_sent_to_bridge(message_size: u32) {
			tracing::trace!(
				target: LOG_TARGET,
				?message_size, "on_message_sent_to_bridge"
			);
			let _ = Bridge::<T, I>::try_mutate(|bridge| {
				let is_channel_with_bridge_hub_congested =
					T::LocalXcmChannelManager::is_congested(&T::SiblingBridgeHubLocation::get());
				let is_bridge_congested = bridge.is_congested;

				// if outbound queue is not congested AND bridge has not reported congestion, do
				// nothing
				if !is_channel_with_bridge_hub_congested && !is_bridge_congested {
					return Err(());
				}

				let previous_factor = Self::get_fee_factor(());
				// ok - we need to increase the fee factor, let's do that
				<Self as FeeTracker>::do_increase_fee_factor(
					&mut bridge.delivery_fee_factor,
					message_size as u128,
				);

				tracing::info!(
					target: LOG_TARGET,
					from=%previous_factor,
					to=%bridge.delivery_fee_factor,
					"Bridge channel is congested. Increased fee factor"
				);
				Self::deposit_event(Event::DeliveryFeeFactorIncreased {
					new_value: bridge.delivery_fee_factor,
				});
				Ok(())
			});
		}
```
