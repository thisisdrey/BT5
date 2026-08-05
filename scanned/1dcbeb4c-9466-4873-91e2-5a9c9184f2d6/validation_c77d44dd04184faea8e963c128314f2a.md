### Title
Inbound Snowbridge message `submit()` decodes attacker-influenced `envelope.payload` with no size bound before dispatch, while charged weight is fixed regardless of payload length - ([File: bridges/snowbridge/pallets/inbound-queue/src/lib.rs])

### Summary
The `snowbridge-pallet-inbound-queue::submit` extrinsic accepts an `EventProof` whose event log contains an arbitrary-length `payload: Vec<u8>` field. Unlike the outbound direction, which explicitly bounds outgoing payloads with `MaxMessagePayloadSize` before enqueueing [1](#0-0) , the inbound path never checks `envelope.payload.len()` against any declared maximum before calling `VersionedMessage::decode_all` and forwarding into XCM conversion and dispatch [2](#0-1) . `T::MaxMessageSize` exists in the pallet config but is only used to *estimate* delivery cost via `Pallet::<T>::get()`, not to reject oversized submissions [3](#0-2) , and the extrinsic's charged weight is the fixed `T::WeightInfo::submit()` regardless of the actual payload/event size [4](#0-3) .

### Finding Description
`submit()` verifies the Ethereum log via `T::Verifier::verify`, decodes it into an `Envelope` (whose `payload` field is an unconstrained `Vec<u8>` populated directly from the ABI-decoded Solidity event `bytes payload`) [5](#0-4) , then immediately decodes that payload into a `VersionedMessage` and proceeds to XCM conversion, fee burning, and `send_xcm` — all gated only by proof validity, not by payload size [2](#0-1) .

The pallet's `#[pallet::weight(T::WeightInfo::submit())]` annotation is a constant, benchmarked for a "typical" message; it does not scale with `event.event_log`/`payload` length the way the pallet-level delivery-cost fee model does (`calculate_delivery_cost` does account for `event.encode().len()`, but that is only used to compute a *balance transfer reward* to the relayer, not to gate weight or reject the call) [6](#0-5) [7](#0-6) .

This mirrors the report's core invariant break — "no limits on the size of text field inputs" leading to DoS/unexpected behavior — mapped onto a public dispatchable: an unbounded, externally-sourced byte blob is decoded and processed under a flat weight charge with no explicit maximum-size `ensure!` guard prior to decode/dispatch.

### Impact Explanation
If the Ethereum-side Gateway contract (or any future/alternate deployment or bug in it) can be made to emit a `payload` far larger than the benchmarked worst case, a relayer can submit a validly-proven event whose decode/conversion/dispatch cost significantly exceeds the weight charged for `submit()`. Because message-queue/dispatch pallets in this codebase generally rely on `#[pallet::weight]` annotations matching actual execution cost to protect block production (as reflected by the explicit `HARD_MESSAGE_SIZE_LIMIT`/`maximal_incoming_message_size` bound enforced in the sibling `bridges/modules/messages` pallet [8](#0-7)  and the explicit `MaxMessagePayloadSize` checks in both Snowbridge outbound queues [9](#0-8) [10](#0-9) ), the absence of an equivalent guard on the inbound v1 path is an inconsistency that can allow underpriced/oversized work to enter block execution, degrading block production — one of the explicitly in-scope impact classes.

### Likelihood Explanation
Likelihood is constrained by the fact that the payload content originates from the Ethereum Gateway contract's emitted event, which is off-repo Solidity code not covered by this scan; this analog only demonstrates that **the Substrate-side pallet itself provides no independent, defense-in-depth size check** before expensive decode/dispatch work, regardless of what upstream contract guarantees exist or change over time. This is a real gap relative to the pattern used everywhere else in the bridge/message pallets in this repository (explicit `ensure!(payload.len() < Max...)` checks), making it a genuine local inconsistency rather than a purely external-protocol assumption.

### Recommendation
Add an explicit `ensure!(envelope.payload.len() as u32 <= T::MaxMessageSize::get(), Error::<T>::InvalidPayload)` (or a similarly named check) in `submit()` immediately after decoding the `Envelope` and before calling `VersionedMessage::decode_all`, matching the pattern already used in `bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs` and `outbound-queue-v2/src/send_message_impl.rs`. Additionally, consider making the weight charged in `submit()` scale with `event.event_log`/`payload` length (similar to how `calculate_delivery_cost` already factors in `event.encode().len()` for the reward calculation) so that oversized-but-proof-valid messages cannot be processed under-priced.

### Proof of Concept
1. An Ethereum Gateway contract instance (legitimate, malicious, or buggy) emits an `OutboundMessageAccepted` event with a `payload` field far exceeding the size implied by `T::MaxMessageSize`.
2. A relayer obtains a valid Merkle/receipt proof for this event (this is normal relayer operation, not privileged) and calls `snowbridge_pallet_inbound_queue::submit(event)`.
3. `T::Verifier::verify` succeeds (the proof is valid for the actual, oversized log) [11](#0-10) .
4. `Envelope::try_from` decodes the oversized `payload` into the `Envelope` struct with no length rejection [12](#0-11) .
5. `VersionedMessage::decode_all(&mut envelope.payload.as_ref())` and subsequent `do_convert`/`send_xcm` execute against this oversized payload under the flat `T::WeightInfo::submit()` charge [2](#0-1) , with no pallet-level guard rejecting it before this expensive work is performed.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-49)
```rust
	fn validate(
		message: &Message,
	) -> Result<(Self::Ticket, Fee<<Self as SendMessageFeeProvider>::Balance>), SendError> {
		// The inner payload should not be too large
		let payload = message.command.abi_encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L235-237)
```rust
		#[pallet::call_index(0)]
		#[pallet::weight(T::WeightInfo::submit())]
		pub fn submit(origin: OriginFor<T>, event: EventProof) -> DispatchResult {
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L241-243)
```rust
			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L271-282)
```rust
			let sovereign_account = sibling_sovereign_account::<T>(channel.para_id);
			let delivery_cost = Self::calculate_delivery_cost(event.encode().len() as u32);
			let amount = T::Token::reducible_balance(
				&sovereign_account,
				Preservation::Preserve,
				Fortitude::Polite,
			)
			.min(delivery_cost);
			if !amount.is_zero() {
				T::Token::transfer(&sovereign_account, &who, amount, Preservation::Preserve)?;
			}

```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L283-301)
```rust
			// Decode payload into `VersionedMessage`
			let message = VersionedMessage::decode_all(&mut envelope.payload.as_ref())
				.map_err(|_| Error::<T>::InvalidPayload)?;

			// Decode message into XCM
			let (xcm, fee) = Self::do_convert(envelope.message_id, message.clone())?;

			tracing::info!(
				target: LOG_TARGET,
				?xcm,
				?fee,
				"💫 xcm decoded"
			);

			// Burning fees for teleport
			Self::burn_fees(channel.para_id, fee)?;

			// Attempt to send XCM to a dest parachain
			let message_id = Self::send_xcm(xcm, channel.para_id)?;
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L343-349)
```rust
		pub fn calculate_delivery_cost(length: u32) -> BalanceOf<T> {
			let weight_fee = T::WeightToFee::weight_to_fee(&T::WeightInfo::submit());
			let len_fee = T::LengthToFee::weight_to_fee(&Weight::from_parts(length as u64, 0));
			weight_fee
				.saturating_add(len_fee)
				.saturating_add(T::PricingParameters::get().rewards.local)
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/lib.rs (L378-384)
```rust
	/// API for accessing the delivery cost of a message
	impl<T: Config> Get<BalanceOf<T>> for Pallet<T> {
		fn get() -> BalanceOf<T> {
			// Cost here based on MaxMessagePayloadSize(the worst case)
			Self::calculate_delivery_cost(T::MaxMessageSize::get())
		}
	}
```

**File:** bridges/snowbridge/pallets/inbound-queue/src/envelope.rs (L11-49)
```rust
sol! {
	event OutboundMessageAccepted(bytes32 indexed channel_id, uint64 nonce, bytes32 indexed message_id, bytes payload);
}

/// An inbound message that has had its outer envelope decoded.
#[derive(Clone, Debug)]
pub struct Envelope {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// The message Channel
	pub channel_id: ChannelId,
	/// A nonce for enforcing replay protection and ordering.
	pub nonce: u64,
	/// An id for tracing the message on its route (has no role in bridge consensus)
	pub message_id: H256,
	/// The inner payload generated from the source application.
	pub payload: Vec<u8>,
}

#[derive(Copy, Clone, Debug)]
pub struct EnvelopeDecodeError;

impl TryFrom<&Log> for Envelope {
	type Error = EnvelopeDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = OutboundMessageAccepted::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| EnvelopeDecodeError)?;

		Ok(Self {
			gateway: log.address,
			channel_id: ChannelId::from(event.channel_id.as_ref()),
			nonce: event.nonce,
			message_id: H256::from(event.message_id.as_ref()),
			payload: event.payload.into(),
		})
	}
```

**File:** bridges/primitives/messages/src/lib.rs (L49-119)
```rust
/// Hard limit on message size that can be sent over the bridge.
pub const HARD_MESSAGE_SIZE_LIMIT: u32 = 64 * 1024;

/// Substrate-based chain with messaging support.
pub trait ChainWithMessages: Chain {
	/// Name of the bridge messages pallet (used in `construct_runtime` macro call) that is
	/// deployed at some other chain to bridge with this `ChainWithMessages`.
	///
	/// We assume that all chains that are bridging with this `ChainWithMessages` are using
	/// the same name.
	const WITH_CHAIN_MESSAGES_PALLET_NAME: &'static str;

	/// Maximal number of unrewarded relayers in a single confirmation transaction at this
	/// `ChainWithMessages`. Unrewarded means that the relayer has delivered messages, but
	/// either confirmations haven't been delivered back to the source chain, or we haven't
	/// received reward confirmations yet.
	///
	/// This constant limits maximal number of entries in the `InboundLaneData::relayers`. Keep
	/// in mind that the same relayer account may take several (non-consecutive) entries in this
	/// set.
	const MAX_UNREWARDED_RELAYERS_IN_CONFIRMATION_TX: MessageNonce;
	/// Maximal number of unconfirmed messages in a single confirmation transaction at this
	/// `ChainWithMessages`. Unconfirmed means that the
	/// message has been delivered, but either confirmations haven't been delivered back to the
	/// source chain, or we haven't received reward confirmations for these messages yet.
	///
	/// This constant limits difference between last message from last entry of the
	/// `InboundLaneData::relayers` and first message at the first entry.
	///
	/// There is no point of making this parameter lesser than
	/// `MAX_UNREWARDED_RELAYERS_IN_CONFIRMATION_TX`, because then maximal number of relayer entries
	/// will be limited by maximal number of messages.
	///
	/// This value also represents maximal number of messages in single delivery transaction.
	/// Transaction that is declaring more messages than this value, will be rejected. Even if
	/// these messages are from different lanes.
	const MAX_UNCONFIRMED_MESSAGES_IN_CONFIRMATION_TX: MessageNonce;

	/// Return maximal dispatch weight of the message we're able to receive.
	fn maximal_incoming_message_dispatch_weight() -> Weight {
		// we leave 1/2 of `max_extrinsic_weight` for the delivery transaction itself
		Self::max_extrinsic_weight() / 2
	}

	/// Return maximal size of the message we're able to receive.
	fn maximal_incoming_message_size() -> u32 {
		maximal_incoming_message_size(Self::max_extrinsic_size())
	}
}

/// Return maximal size of the message the chain with `max_extrinsic_size` is able to receive.
pub fn maximal_incoming_message_size(max_extrinsic_size: u32) -> u32 {
	// The maximal size of extrinsic at Substrate-based chain depends on the
	// `frame_system::Config::MaximumBlockLength` and
	// `frame_system::Config::AvailableBlockRatio` constants. This check is here to be sure that
	// the lane won't stuck because message is too large to fit into delivery transaction.
	//
	// **IMPORTANT NOTE**: the delivery transaction contains storage proof of the message, not
	// the message itself. The proof is always larger than the message. But unless chain state
	// is enormously large, it should be several dozens/hundreds of bytes. The delivery
	// transaction also contains signatures and signed extensions. Because of this, we reserve
	// 1/3 of the the maximal extrinsic size for this data.
	//
	// **ANOTHER IMPORTANT NOTE**: large message means not only larger proofs and heavier
	// proof verification, but also heavier message decoding and dispatch. So we have a hard
	// limit of `64Kb`, which in practice limits the message size on all chains. Without this
	// limit the **weight** (not the size) of the message will be higher than the
	// `Self::maximal_incoming_message_dispatch_weight()`.

	sp_std::cmp::min(max_extrinsic_size / 3 * 2, HARD_MESSAGE_SIZE_LIMIT)
}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-29)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);
```
