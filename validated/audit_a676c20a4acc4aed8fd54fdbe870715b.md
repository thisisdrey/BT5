Audit Report

## Title
Relayer reward paid regardless of message dispatch outcome due to unchecked `success` field - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The `submit_delivery_receipt` extrinsic decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` struct that includes a `success: bool` field, but `process_delivery_receipt` never inspects this field before paying the relayer reward and deleting the `PendingOrder`. Any relayer who can produce a valid proof for the event — regardless of whether the dispatched command actually succeeded on Ethereum — collects the full fee.

## Finding Description
The Ethereum-side event `InboundMessageDispatched(nonce, topic, success, reward_address)` is decoded verbatim into `DeliveryReceipt`, preserving the `success` flag: [1](#0-0) , with the decoding performed in `TryFrom<&Log>`: [2](#0-1) .

`submit_delivery_receipt` verifies the proof via `T::Verifier::verify` (which only attests that the log genuinely occurred and is included, not the semantic outcome), decodes the receipt, and forwards it to `process_delivery_receipt`: [3](#0-2) .

`process_delivery_receipt` checks only the gateway address and the existence of a `PendingOrder` for the nonce; it never reads `receipt.success` before calling `T::RewardPayment::register_reward` and removing the order: [4](#0-3) . The `PendingOrder` is originally inserted with the fee at message-enqueue time: [5](#0-4) .

This violates the settlement invariant that payout should only advance after the underlying operation (dispatch on Ethereum) actually succeeds — the field exists specifically to signal that outcome but is structurally discarded.

## Impact Explanation
This is a reward/accounting-conservation violation: fee is paid unconditionally on proof-of-event rather than proof-of-success, and the `PendingOrder` is permanently removed regardless of outcome, precluding any later correction. This matches the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot, and constitutes a public underpriced/incorrect-payout condition in bridge processing.

## Likelihood Explanation
Any unprivileged, signed account can invoke `submit_delivery_receipt` with a legitimately provable event log for a nonce whose Ethereum-side command execution failed (e.g., reverted inside the Gateway's command dispatch), and still receive `order.fee`, since neither `T::Verifier::verify` nor `process_delivery_receipt` gates on the `success` flag. No governance, validator, or privileged role is required.

## Recommendation
In `process_delivery_receipt` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`), branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success` is `true`. For failed deliveries, still remove/mark the `PendingOrder` to prevent replay, but emit a distinct event (e.g., `MessageDeliveryFailed`) and skip reward payment, so success becomes a first-class condition of settlement.

## Proof of Concept
1. A message is enqueued via `do_process_message`, inserting a `PendingOrder` with `fee > 0` at a given `nonce` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:426-436`).
2. The corresponding command dispatch reverts on the Ethereum Gateway, which emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid receipt/inclusion proof for this event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the event genuinely occurred), `DeliveryReceipt::try_from` decodes `success=false`, but `process_delivery_receipt` pays `order.fee` to the relayer and removes the `PendingOrder` regardless (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:446-480`), demonstrating reward paid for a failed delivery.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L300-317)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
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
