The claim is confirmed against the repository code — verified independently.

Audit Report

## Title
`process_delivery_receipt` pays relayer reward and finalizes order regardless of Ethereum dispatch outcome, ignoring `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
The `DeliveryReceipt` decoded from the `InboundMessageDispatched` Ethereum event carries a `success` field indicating whether the message's commands actually executed on Ethereum, but `Pallet::process_delivery_receipt` never inspects it before paying the relayer reward and deleting the `PendingOrder`. As a result, a relayer who submits a genuine, correctly-proved receipt for a failed Ethereum-side dispatch (`success: false`) is paid the full fee exactly as if the delivery succeeded, and the order state is permanently discarded with no retry path.

## Finding Description
`DeliveryReceipt` explicitly tracks a `success: bool` field decoded from the `InboundMessageDispatched` Solidity event [1](#0-0) . `submit_delivery_receipt` verifies the proof and decodes the receipt via `DeliveryReceipt::try_from`, then forwards it unconditionally to `process_delivery_receipt` [2](#0-1) . Inside `process_delivery_receipt`, only the gateway address and existence of a `PendingOrder` for the nonce are checked; `receipt.success` is never read before `T::RewardPayment::register_reward` is called, `PendingOrders::remove(nonce)` deletes the order, and `Event::MessageDelivered` is emitted [3](#0-2) . A grep across the entire `outbound-queue-v2` pallet confirms `success` is referenced nowhere else, so no other guard exists.

## Impact Explanation
This matches the "duplicate settlement or payout" / "public underpriced work" impact class: any signed account can collect the full `order.fee` for a message whose Ethereum-side commands reverted, since `submit_delivery_receipt` is a public, unprivileged extrinsic and `success: false` is a normal (non-adversarial) outcome (e.g. insufficient `GasMeter` allowance, a reverting command). The `PendingOrder` is also permanently removed via `PendingOrders::remove(nonce)`, precluding any retry/resend mechanism for the failed message — a permanent bridge-state loss of tracking for that nonce.

## Likelihood Explanation
High: `submit_delivery_receipt` requires only `ensure_signed` and a valid Merkle/verifier proof of a real event log [4](#0-3) ; failure receipts are an expected, non-malicious outcome per the module's own documentation of the delivery pipeline [5](#0-4) , so no adversarial setup is needed to trigger the bug.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; for `success == false`, emit a distinct event (e.g. `MessageDeliveryFailed { nonce }`) and decide explicitly whether to retain the `PendingOrder` for retry or handle it through a dedicated failure-settlement path instead of silently paying and discarding state as if delivery succeeded.

## Proof of Concept
1. A message is enqueued via `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0` [6](#0-5) .
2. On Ethereum, the Gateway executes the message but a command reverts, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any signed account calls `submit_delivery_receipt` with a valid proof of this genuine, unmodified event log.
4. `T::Verifier::verify` succeeds, `DeliveryReceipt::try_from` decodes `success: false` correctly [7](#0-6) .
5. `process_delivery_receipt` pays the full `order.fee` reward, removes `PendingOrders[nonce]`, and emits `Event::MessageDelivered { nonce }` — identical to the success path, despite `receipt.success == false` [8](#0-7) .

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L38-51)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
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
