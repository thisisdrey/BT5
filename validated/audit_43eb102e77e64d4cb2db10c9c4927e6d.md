Confirmed: no occurrence of `success` anywhere in the `outbound-queue-v2` pallet code, verifying that `receipt.success` is decoded but never referenced/checked in `process_delivery_receipt`.The module doc even describes the intended flow: "Fetch the pending order by nonce of the message, pay reward with fee attached in the order... Remove the order" — with no mention of checking success. This confirms the design gap matches the claim exactly.

Audit Report

## Title
Relayer reward paid and delivery order settled without checking the on-chain `success` flag in the Ethereum delivery receipt - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` struct that carries a `success: bool` field [1](#0-0)  but never reads that field before paying the relayer reward and removing the pending order [2](#0-1) . A search of the entire pallet confirms `success` is never referenced anywhere in `outbound-queue-v2`, meaning the field decoded from the real Ethereum log is completely discarded before the settlement logic runs.

## Finding Description
The public extrinsic `submit_delivery_receipt` verifies the Ethereum proof and decodes the event log into a `DeliveryReceipt` via `DeliveryReceipt::try_from`, then unconditionally forwards it to `process_delivery_receipt` [3](#0-2) . The decoder in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` faithfully copies `event.success` from the ABI-decoded Solidity event into `DeliveryReceipt.success` [4](#0-3) .

`process_delivery_receipt` then only checks `T::GatewayAddress::get() == receipt.gateway` and that a `PendingOrder` exists for `receipt.nonce`; it never inspects `receipt.success` before calling `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and unconditionally removing the order from `PendingOrders` [5](#0-4) . The pallet's own module documentation describes this exact flow — "Fetch the pending order by nonce ... pay reward with fee attached in the order ... Remove the order" — with no mention of an outcome check [6](#0-5) . Since `InboundMessageDispatched` is emitted by the Ethereum gateway regardless of whether the dispatched command actually succeeded (the `success` flag exists precisely to distinguish these cases), any genuine event with `success = false` still passes proof verification and is treated identically to a successful delivery.

## Impact Explanation
This causes an unbacked/incorrect reward payout via `T::RewardPayment::register_reward` for deliveries Ethereum itself reports as failed, and it permanently removes the corresponding `PendingOrder` from `PendingOrders`, precluding any retry, reconciliation, or correct re-settlement of the failed message. This matches the "duplicate settlement or payout" / bridge-state-must-only-advance-after-execution-succeeds impact category, since the queue's settlement marker (`PendingOrders`) advances without a corresponding successful Ethereum-side execution.

## Likelihood Explanation
The trigger requires no malicious behavior: an inbound message dispatch on the Ethereum gateway can legitimately fail (e.g., insufficient gas or a revert in the target execution) while the gateway contract still emits `InboundMessageDispatched` with `success = false`. Any relayer submitting this real, unmodified on-chain event through the public `submit_delivery_receipt` extrinsic will pass `T::Verifier::verify` (a genuine proof) and hit the unconditional reward/removal path, since nothing in `process_delivery_receipt` branches on `success`.

## Recommendation
Branch on `receipt.success` inside `process_delivery_receipt`: only call `T::RewardPayment::register_reward` and `<PendingOrders<T>>::remove(nonce)` when `success == true`. On `success == false`, emit a distinct event (e.g., `MessageDeliveryFailed`) and implement an explicit failure-handling policy (retry, refund, or move to a failed-order state) instead of silently deleting the order and rewarding the relayer as if delivery succeeded.

## Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }` entry in `PendingOrders` [7](#0-6) .
2. On Ethereum, the gateway attempts to dispatch this message but the dispatch fails; the contract still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid proof for this real event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (genuine proof), and `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` proceeds regardless: `order.fee > 0` triggers `register_reward`, `PendingOrders::<T>::remove(nonce)` executes, and `Event::MessageDelivered { nonce }` is emitted — identical to the genuine success path exercised in `submit_delivery_receipt_succeeds_after_unhalt` [8](#0-7) , despite the underlying Ethereum dispatch having failed.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L14-27)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-42)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
//!
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L418-449)
```rust
// After governance resumes the bridge, legitimate delivery receipts flow through again:
// the order is paid out and removed from storage.
#[test]
fn submit_delivery_receipt_succeeds_after_unhalt() {
	new_tester().execute_with(|| {
		let nonce = 0;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_valid_event_proof());

		// Bridge halted — receipt rejected, order untouched.
		set_verifier_halted(true);
		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);
		assert!(PendingOrders::<Test>::get(nonce).is_some());

		// Bridge resumed — same receipt succeeds and the order is settled.
		set_verifier_halted(false);
		assert_ok!(OutboundQueue::submit_delivery_receipt(origin, event));
		assert!(PendingOrders::<Test>::get(nonce).is_none());

		System::assert_has_event(mock::RuntimeEvent::OutboundQueue(Event::MessageDelivered {
			nonce,
		}));
	});
}
```
