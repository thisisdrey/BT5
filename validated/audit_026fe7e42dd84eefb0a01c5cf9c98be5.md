### Title
`submit_delivery_receipt` pays the relayer reward regardless of the on-chain `success` flag in the delivery receipt - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The Snowbridge V2 outbound queue's `process_delivery_receipt` decodes an Ethereum-emitted `InboundMessageDispatched` event that explicitly carries a `success: bool` field, but the pallet logic never inspects that field before paying out the relayer reward. Any relayer who submits a valid merkle/beacon proof for a receipt — even one where the corresponding message execution on Ethereum failed — will still have `order.fee` registered as a reward and the `PendingOrder` removed.

### Finding Description
`DeliveryReceipt` is decoded from the `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` Solidity event log [1](#0-0) , and the `success` value is faithfully copied into the Rust struct during decoding [2](#0-1) .

However, `Pallet::process_delivery_receipt` only checks the `gateway` address against `T::GatewayAddress`, looks up the `PendingOrder` by `nonce`, and unconditionally pays the fee if it's non-zero — it never reads or gates on `receipt.success`: [3](#0-2) 

The extrinsic `submit_delivery_receipt` itself only verifies the merkle/beacon proof of the event log and decodes the receipt before calling `process_delivery_receipt` — it does not perform any success check either: [4](#0-3) 

This is a real local analog to the report's core broken invariant: an on-chain payout decision ("did the message actually execute successfully / does this reward apply") is meant to be represented by a specific field in the underlying verified data (`success`), but the code silently ignores that field and instead trusts that a valid proof alone means the delivery outcome is what the relayer/caller implies. Any signed account (`ensure_signed(origin)?`, no special privilege required) can submit a delivery receipt for a message that reverted or otherwise failed on Ethereum and still collect the full fee, because nothing in the dispatch path conditions the `register_reward` call on `success == true`.

### Impact Explanation
This causes a duplicate/incorrect settlement: the relayer reward pot pays out `order.fee` for message deliveries that did not actually succeed on Ethereum, decoupling on-chain reward accounting from the true on-chain (Ethereum-side) execution outcome — directly analogous to the report's "cutting board weights disconnect from onchain voting outcomes," except here the disconnect is enforced by the pallet's own code, not by an offchain/trusted keeper input. This drains the reward pot for work that wasn't actually completed, and removes the `PendingOrder`, permanently foreclosing any future correct resolution for that nonce (state can only be settled once per nonce, and it gets consumed incorrectly).

### Likelihood Explanation
High from an implementation standpoint since no privileged actor or malicious relayer collusion is needed — any relayer submitting proofs for genuinely emitted (but `success: false`) events benefits automatically. This does not require a malicious peer, prover, or admin: the vulnerability is purely in the pallet's dispatch logic failing to consult data it already decoded from a correctly verified proof.

### Recommendation
Gate the reward payment on `receipt.success`:
```rust
if receipt.success && order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
```
If a failed delivery should still allow order cleanup (e.g., to avoid the pending order growing forever), separate the "resolve/close order" logic from "pay reward" logic, and emit an event to distinguish `MessageDelivered { nonce, success }` from a no-reward resolution, so downstream analytics and re-delivery logic can react correctly.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { fee: F, .. }` [5](#0-4) .
2. On Ethereum, message execution for `nonce` reverts/fails; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` (this is standard Solidity event-log behavior for delivery outcome reporting).
3. Any account constructs a valid beacon/merkle proof for this genuine event log and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the log is real and correctly proven), `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` never inspects it: `order.fee > 0` is true, so `T::RewardPayment::register_reward` credits the reward to `reward_account`, and the `PendingOrder` is removed via `<PendingOrders<T>>::remove(nonce)` [6](#0-5) .
5. The relayer is paid in full for a message delivery that did not succeed, and the order can never be resubmitted/corrected since it's removed from storage.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-27)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}

/// Delivery receipt
#[derive(Clone, Debug)]
pub struct DeliveryReceipt {
	/// The address of the outbound queue on Ethereum that emitted this message as an event log
	pub gateway: H160,
	/// The nonce of the dispatched message
	pub nonce: u64,
	/// Message topic
	pub topic: H256,
	/// Delivery status
	pub success: bool,
	/// The reward address
	pub reward_address: [u8; 32],
}
```

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-51)
```rust
impl TryFrom<&Log> for DeliveryReceipt {
	type Error = DeliveryReceiptDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-317)
```rust
		#[pallet::call_index(1)]
		#[pallet::weight(T::WeightInfo::submit_delivery_receipt())]
		pub fn submit_delivery_receipt(
			origin: OriginFor<T>,
			event: Box<EventProof>,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			let relayer = ensure_signed(origin)?;

			// submit message to verifier for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			let receipt = DeliveryReceipt::try_from(&event.event_log)
				.map_err(|_| Error::<T>::InvalidEnvelope)?;

			Self::process_delivery_receipt(relayer, receipt)
		}
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-480)
```rust
		/// Process a delivery receipt from a relayer, to allocate the relayer reward.
		pub fn process_delivery_receipt(
			relayer: <T as frame_system::Config>::AccountId,
			receipt: DeliveryReceipt,
		) -> DispatchResult
		where
			<T as frame_system::Config>::AccountId: From<[u8; 32]>,
		{
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);

			let reward_account = if receipt.reward_address == [0u8; 32] {
				relayer
			} else {
				receipt.reward_address.into()
			};

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

			<PendingOrders<T>>::remove(nonce);

			Self::deposit_event(Event::MessageDelivered { nonce });

			Ok(())
		}
```
