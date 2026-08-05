Based on the analysis, the strongest real analog is in the Snowbridge V2 outbound queue: `Message.fee` is a value fully controlled by whoever constructs the message (the `EthereumBlobExporter`/system-v2 pallet callers), and it is taken **as-is** and stored into `PendingOrders` without any independent validation against the pallet's own pricing/weight-based fee computation.

### Title
Outbound Queue V2 accepts attacker/caller-supplied `fee` with no minimum-price enforcement, allowing zero-fee relayer reward orders - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::do_process_message` in `snowbridge-pallet-outbound-queue-v2` decodes an already-enqueued `Message { origin, id, fee, commands }` and stores `fee` verbatim into `PendingOrders<T>` as the reward that will later be paid to the relayer who submits the delivery receipt. Unlike the V1 outbound queue, which computes the fee itself from `T::GasMeter`/`T::PricingParameters`/`T::WeightToFee` (`calculate_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs:368-393`), the V2 pallet has **no analogous `calculate_fee` call and no minimum-fee check** in `do_process_message`. The `fee` field is simply trusted from the encoded `Message`. [1](#0-0) [2](#0-1) 

### Finding Description
This mirrors the external report's core invariant break: two logical paths reference "the price/fee that should be charged," but only one of them enforces/derives it from an authoritative source, while the other blindly trusts a caller-provided value that defaults (or can be set) to zero.

- V1 queue: fee is *computed* by the pallet from `GasMeter`, `PricingParameters`, and `WeightToFee` — see `calculate_fee`/`calculate_remote_fee`/`calculate_local_fee` in `bridges/snowbridge/pallets/outbound-queue/src/lib.rs:368-418`, and `validate()` in `send_message_impl.rs` calls `Self::calculate_fee(...)` before creating the ticket. [3](#0-2) 
- V2 queue: `validate()` in `bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs` only checks payload size and returns the `Message` clone as the ticket — it never calls any fee-calculation routine, and `fee` is simply whatever value was placed in the `Message` struct by the caller (e.g., `EthereumSystemFrontend`/system-v2 pallet or the XCM exporter). [4](#0-3) 
- `do_process_message` then takes that same untrusted `fee` and stores it directly as `PendingOrder.fee`, which is later paid out in `process_delivery_receipt` via `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` only `if order.fee > 0`. [5](#0-4) 

There is no code path in `outbound-queue-v2` that cross-checks the supplied `fee` against a `PricingParameters`-derived minimum (as V1 does), and no `Config` item wiring a minimum/expected fee into `do_process_message`. Any component able to enqueue a `Message` into this pallet's `AggregateMessageOrigin` queue (fully permissionless once the message reaches the `MessageQueue`) can set `fee: 0`, causing the message to be committed and delivered on Ethereum while no relayer reward is ever registered for the eventual delivery-receipt submitter — a direct parallel to `GigaNameNFT.mintUsername()` referencing an uninitialized/zero price column instead of the authoritative one used by the correctly-priced path (`AccountSystem.mintWithEth`).

### Impact Explanation
If the fee is set to (or defaults to) zero, relayers doing the actual work of delivering messages to Ethereum and submitting delivery receipts back to BridgeHub receive no reward. This degrades bridge processing incentives ("public underpriced work that... stalls bridge processing" per the impact gate) — relayers have no economic incentive to relay legitimate messages, potentially stalling the Snowbridge delivery pipeline, while the message itself still gets committed and forwarded to Ethereum for free. This is distinct from a governance/admin misconfiguration because the fee is set per-message by the caller constructing the `Message`, not by a privileged pricing-parameter update.

### Likelihood Explanation
Medium: reaching `do_process_message` requires successfully enqueuing a `Message` via `SendMessage::validate`/`deliver`, which in turn requires the message to originate from an authorized channel/origin per the runtime's `EnqueueMessage` configuration for the `AggregateMessageOrigin`. If any caller of `deliver()` (e.g., system-v2 pallet's registration/token calls, or the V2 XCM exporter) does not itself enforce a non-zero/paid `fee` before constructing the `Message`, the zero-fee path is trivially reachable without any privileged action — only an ordinary user-triggered send.

### Recommendation
Have `outbound-queue-v2`'s `validate()` (or `do_process_message`) independently compute a minimum required fee (mirroring V1's `calculate_fee` using `GasMeter`/pricing parameters/`WeightToFee`) and `ensure!(fee >= minimum_fee, Error::InsufficientFee)` before enqueueing/committing the message, so the reward stored in `PendingOrders` can never be pushed below the pallet's own authoritative pricing — exactly as the report recommends: ensure both paths (the one charging normally, and this one) reference the same, single source-of-truth price.

### Proof of Concept
1. A caller with access to construct a `snowbridge_outbound_queue_primitives::v2::Message` (e.g., through the system-v2 pallet's send path or XCM exporter) builds a `Message { origin, id, fee: 0, commands: [...] }`.
2. `SendMessage::validate` in `send_message_impl.rs` only checks payload size and passes the message through unchanged. [4](#0-3) 
3. `deliver()` enqueues it into `T::MessageQueue`. [6](#0-5) 
4. `do_process_message` decodes the message, and stores `PendingOrder { nonce, fee: 0, block_number }`. [2](#0-1) 
5. After the message is relayed to Ethereum and executed, a relayer calls `submit_delivery_receipt` → `process_delivery_receipt`, which reads `order.fee == 0` and skips `register_reward` entirely (`if order.fee > 0`). [5](#0-4) 
6. Result: the message was committed, delivered, and executed on Ethereum, but the relayer receives zero reward — the "public work" of delivering the bridge message was performed for free, exactly analogous to `mintUsername()` minting for free due to referencing an unenforced zero price.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L360-369)
```rust
			// Decode bytes into Message
			let Message { origin, id, fee, commands } =
				Message::decode(&mut message).map_err(|_| {
					Self::deposit_event(Event::MessageRejected {
						id: None,
						payload: message.to_vec(),
						error: Corrupt,
					});
					Corrupt
				})?;
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
```rust
			// Generate `PendingOrder` with fee attached in the message, stored
			// into the `PendingOrders` map storage, with assigned nonce as the key.
			// When the message is processed on ethereum side, the relayer will send the nonce
			// back with delivery proof, only after that the order can
			// be resolved and the fee will be rewarded to the relayer.
			let order = PendingOrder {
				nonce,
				fee,
				block_number: frame_system::Pallet::<T>::current_block_number(),
			};
			<PendingOrders<T>>::insert(nonce, order);
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L462-473)
```rust
			let nonce = receipt.nonce;

			let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;

			if order.fee > 0 {
				// Pay relayer reward
				T::RewardPayment::register_reward(
					&reward_account,
					T::DefaultRewardKind::get(),
					order.fee,
				);
			}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-61)
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

		// Ensure there is a registered channel we can transmit this message on
		ensure!(T::Channels::contains(&message.channel_id), SendError::InvalidChannel);

		// Generate a unique message id unless one is provided
		let message_id: H256 = message
			.id
			.unwrap_or_else(|| unique((message.channel_id, &message.command)).into());

		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());

```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L23-32)
```rust
	fn validate(message: &Message) -> Result<Self::Ticket, SendError> {
		// The inner payload should not be too large
		let payload = message.encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		Ok(message.clone())
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/send_message_impl.rs (L34-43)
```rust
	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = ticket.origin.into();

		let message =
			BoundedVec::try_from(ticket.encode()).map_err(|_| SendError::MessageTooLarge)?;

		T::MessageQueue::enqueue_message(message.as_bounded_slice(), origin);
		Self::deposit_event(Event::MessageQueued { message: ticket.clone() });
		Ok(ticket.id)
	}
```
