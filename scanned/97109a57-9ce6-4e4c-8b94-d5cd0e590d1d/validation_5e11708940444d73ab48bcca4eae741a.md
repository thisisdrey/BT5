Based on the investigation, the strongest local analog to the reported cross-chain gas-limit DoS is in the Substrate **bridge-messages pallet**, where message delivery is strictly ordered by nonce and the destination-chain dispatch weight budget is validated only against an *unenforced, off-chain/test-time* assumption rather than an on-chain guard.

### Title
Un-enforced dispatch-weight/gas assumption between bridged chains can permanently jam a strictly-ordered bridge lane — ([File: bridges/modules/messages/src/lib.rs])

### Summary
`pallet-bridge-messages` requires messages on a lane to be delivered strictly in nonce order — nonce `N+1` can never be delivered before `N`. Each message carries a dispatch weight estimate produced by `T::MessageDispatch::dispatch_weight()`, which relayers must declare in `receive_messages_proof()`. If that declared/required weight for a given message ever exceeds the *destination* chain's actual per-extrinsic weight budget, no relayer can ever construct a valid delivery transaction for that nonce: a transaction declaring the true weight is rejected by the block builder/weight limits, while any lower declaration fails with `InsufficientDispatchWeight`. Because the lane enforces strict FIFO delivery, this single message permanently blocks the lane — mirroring the reported bug, where an out-of-gas/over-weight message on the destination chain causes the ORDERED channel to be closed and later packets to be undeliverable.

### Finding Description
- `InboundLane::receive_message` enforces strict ordering: any nonce that is not `last_delivered_nonce + 1` is rejected as `InvalidNonce`. [1](#0-0) 
- `receive_messages_proof` checks that the relayer-declared dispatch weight for the *next* in-order message fits within the remaining weight of the call; if it doesn't, the whole extrinsic fails and the lane makes no progress: [2](#0-1) 
- The message's required dispatch weight is computed by the pluggable `MessageDispatch::dispatch_weight` trait method, whose contract explicitly says it "must return correct upper bound of dispatch weight" matching what's needed to actually dispatch the message. [3](#0-2) 
- The only safeguard that this weight can never exceed what a destination-chain extrinsic can carry is a **test/integrity-check helper**, `check_message_lane_weights` / `ensure_maximal_message_dispatch` / `ensure_able_to_receive_message`, which asserts `message_dispatch_weight <= max_incoming_message_dispatch_weight <= max_extrinsic_weight` — but this is only exercised in runtime dev tests, not enforced on-chain at message-send time. [4](#0-3) [5](#0-4) 
- Outbound `send_message`/`validate_message` on the source chain only checks payload *size* against `MaximalOutboundPayloadSize`; it performs no check that the message's dispatch weight is within what the destination chain can service in a single extrinsic. [6](#0-5) 

This is the direct structural analog of the reported bug: the two chains (source/sender vs. destination/dispatcher) can have divergent weight ("gas") ceilings, and nothing on-chain enforces that a message accepted on one side can ever be dispatched on the other. Just like the "ORDERED" IBC channel that closes on timeout and blocks all future packets, the bridge-messages lane's strict-nonce design means one such mismatched message blocks the entire lane forever, since no future nonce can be processed until it is.

### Impact Explanation
A permanently stuck bridge lane is a Denial-of-Service on message routing/delivery — degrading or halting bridge processing exactly as called out in the impact gate ("public underpriced work that degrades block production or stalls bridge processing"). Every message queued behind the stuck nonce (including reward/settlement-bearing messages, asset transfers, etc.) becomes permanently undeliverable, which can also lead to permanent fund lock for anything conditioned on message delivery (e.g., reserve-backed transfers awaiting confirmation).

### Likelihood Explanation
Triggering requires only that a message be sent whose calculated dispatch weight is close to or exceeds the destination chain's per-extrinsic weight ceiling — this can occur from ordinary configuration drift (weight benchmarks differing between the two chains' runtimes, or a chain upgrade changing effective weight-per-byte without corresponding change on the bridged side) rather than any malicious relayer/validator action, since the guard rails (`ensure_maximal_message_dispatch`, etc.) are dev-time assertions, not runtime-enforced invariants. No privileged actor or malicious peer is needed — an ordinary sender using whatever public path feeds into `MessagesBridge::send_message` (e.g. XCM export over a configured bridge) can produce oversized-payload/weight messages that are accepted by the source-chain size check yet undeliverable at the destination.

### Recommendation
Enforce the dispatch-weight/gas ceiling check at message *send* time on-chain (in `validate_message`/`send_message`), not only as an offline integrity-check helper — reject any outbound message whose `MessageDispatch::dispatch_weight` estimate (or a conservative upper bound thereof) exceeds the configured `BridgedChain::max_extrinsic_weight()` (minus overhead), mirroring the external report's recommendation to configure and validate per-chain weight/gas limits before acceptance rather than discovering the mismatch only at delivery/timeout.

### Proof of Concept
1. Configure/observe two bridged chains where the destination chain's practical `max_extrinsic_weight()` is meaningfully smaller than what the source chain's `MaximalOutboundPayloadSize`/weight benchmarks allow to be declared for `MessageDispatch::dispatch_weight` (e.g., differing weight-per-byte constants from independent benchmarking, or an unaudited runtime upgrade on one side).
2. Send a message via the source chain whose payload passes `MaximalOutboundPayloadSize` but whose computed `dispatch_weight` on the destination exceeds `max_extrinsic_weight()`.
3. Any relayer attempts `receive_messages_proof()` on the destination chain for that nonce: declaring the true weight makes the extrinsic itself un-includable (over max extrinsic weight); declaring anything less trips `InsufficientDispatchWeight` at `bridges/modules/messages/src/lib.rs:294-304`.
4. Because `InboundLane::receive_message` requires strict nonce order (`bridges/modules/messages/src/inbound_lane.rs:193-195`), this nonce can never be skipped, and every subsequent nonce on the lane is permanently blocked — reproducing the "ORDERED channel closes / DoS" scenario from the original report.

### Citations

**File:** bridges/modules/messages/src/inbound_lane.rs (L185-196)
```rust
	/// Receive new message.
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

```

**File:** bridges/modules/messages/src/lib.rs (L290-304)
```rust
				// ensure that relayer has declared enough weight for dispatching next message
				// on this lane. We can't dispatch lane messages out-of-order, so if declared
				// weight is not enough, let's move to next lane
				let message_dispatch_weight = T::MessageDispatch::dispatch_weight(&mut message);
				if message_dispatch_weight.any_gt(dispatch_weight_left) {
					tracing::trace!(
						target: LOG_TARGET,
						?lane_id,
						declared=%message_dispatch_weight,
						left=%dispatch_weight_left,
						"Cannot dispatch any more messages"
					);

					fail!(Error::<T, I>::InsufficientDispatchWeight);
				}
```

**File:** bridges/modules/messages/src/lib.rs (L680-705)
```rust
impl<T, I> bp_messages::source_chain::MessagesBridge<T::OutboundPayload, T::LaneId> for Pallet<T, I>
where
	T: Config<I>,
	I: 'static,
{
	type Error = Error<T, I>;
	type SendMessageArgs = SendMessageArgs<T, I>;

	fn validate_message(
		lane_id: T::LaneId,
		message: &T::OutboundPayload,
	) -> Result<SendMessageArgs<T, I>, Self::Error> {
		// we can't accept any messages if the pallet is halted
		ensure_normal_operating_mode::<T, I>()?;

		// check lane
		let lane = active_outbound_lane::<T, I>(lane_id)?;

		Ok(SendMessageArgs {
			lane_id,
			lane,
			payload: StoredMessagePayload::<T, I>::try_from(message.encode()).map_err(|_| {
				Error::<T, I>::MessageRejectedByPallet(VerificationError::MessageTooLarge)
			})?,
		})
	}
```

**File:** bridges/primitives/messages/src/target_chain.rs (L110-117)
```rust
	/// Estimate dispatch weight.
	///
	/// This function must return correct upper bound of dispatch weight. The return value
	/// of this function is expected to match return value of the corresponding
	/// `From<Chain>InboundLaneApi::message_details().dispatch_weight` call.
	fn dispatch_weight(
		message: &mut DispatchMessage<Self::DispatchPayload, Self::LaneId>,
	) -> Weight;
```

**File:** bridges/bin/runtime-common/src/integrity.rs (L331-365)
```rust
/// Check that the message lane weights are correct.
pub fn check_message_lane_weights<
	C: ChainWithMessages,
	T: frame_system::Config + pallet_bridge_messages::Config<MessagesPalletInstance>,
	MessagesPalletInstance: 'static,
>(
	bridged_chain_extra_storage_proof_size: u32,
	this_chain_max_unrewarded_relayers: MessageNonce,
	this_chain_max_unconfirmed_messages: MessageNonce,
	// whether `RefundBridgedParachainMessages` extension is deployed at runtime and is used for
	// refunding this bridge transactions?
	//
	// in other words: pass true for all known production chains
	runtime_includes_refund_extension: bool,
) {
	type Weights<T, MI> = <T as pallet_bridge_messages::Config<MI>>::WeightInfo;

	// check basic weight assumptions
	pallet_bridge_messages::ensure_weights_are_correct::<Weights<T, MessagesPalletInstance>>();

	// check that the maximal message dispatch weight is below hardcoded limit
	pallet_bridge_messages::ensure_maximal_message_dispatch::<Weights<T, MessagesPalletInstance>>(
		C::maximal_incoming_message_size(),
		C::maximal_incoming_message_dispatch_weight(),
	);

	// check that weights allow us to receive messages
	let max_incoming_message_proof_size =
		bridged_chain_extra_storage_proof_size.saturating_add(C::maximal_incoming_message_size());
	pallet_bridge_messages::ensure_able_to_receive_message::<Weights<T, MessagesPalletInstance>>(
		C::max_extrinsic_size(),
		C::max_extrinsic_weight(),
		max_incoming_message_proof_size,
		C::maximal_incoming_message_dispatch_weight(),
	);
```

**File:** bridges/modules/messages/src/weights_ext.rs (L82-93)
```rust
/// Ensure that we are able to dispatch maximal size messages.
pub fn ensure_maximal_message_dispatch<W: WeightInfoExt>(
	max_incoming_message_size: u32,
	max_incoming_message_dispatch_weight: Weight,
) {
	let message_dispatch_weight = W::message_dispatch_weight(max_incoming_message_size);
	assert!(
		message_dispatch_weight.all_lte(max_incoming_message_dispatch_weight),
		"Dispatch weight of maximal message {message_dispatch_weight:?} must be lower \
		than the hardcoded {max_incoming_message_dispatch_weight:?}",
	);
}
```
