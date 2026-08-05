Audit Report

## Title
Outbound queue v2 pays relayer reward and closes the pending order without checking the delivery `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field indicating whether the relayed message dispatch actually succeeded on Ethereum [1](#0-0) , but `Pallet::process_delivery_receipt` never reads or branches on this field, unconditionally paying the relayer reward and removing the `PendingOrder` regardless of the delivery outcome [2](#0-1) .

## Finding Description
The pallet doc explicitly describes the intended flow: verify the proof, fetch the pending order by nonce, "pay reward with fee attached in the order," then remove the order [3](#0-2) . `submit_delivery_receipt` is a permissionless, signed extrinsic that only requires a valid `EventProof` verified by `T::Verifier::verify` before decoding the log into a `DeliveryReceipt` and calling `process_delivery_receipt` [4](#0-3) .

Inside `process_delivery_receipt`, the code checks `receipt.gateway`, derives the `reward_account`, fetches the `order` by `receipt.nonce`, and then unconditionally calls `T::RewardPayment::register_reward` (when `order.fee > 0`) and unconditionally removes `PendingOrders[nonce]` [5](#0-4) . The `receipt.success` field, which is populated directly from the Solidity event's `success` bool during decoding [6](#0-5) , is never referenced anywhere in this function or the rest of the pallet — confirmed by the absence of any `success` usage in the pallet's own source beyond the struct definition file. The code path taken is identical whether Ethereum reports the inbound dispatch as `success = true` or `success = false`.

## Impact Explanation
This violates the settlement invariant that payout and queue-marker state must only advance after execution/settlement genuinely succeeds. A relayer is rewarded and the `PendingOrder` is permanently deleted even when the underlying Ethereum-side command execution failed (`success:false`, e.g., due to insufficient destination gas or an agent execution revert), leaving no record to retry, re-fee, or reconcile the failed message. This is an underpriced/incorrect-settlement condition on a public extrinsic: relayer rewards are disbursed independent of the correctness signal the receipt was designed to carry, matching the "public underpriced work" / "duplicate or incorrect payout" impact category.

## Likelihood Explanation
Any signed account can call `submit_delivery_receipt` as soon as a genuine (not forged) Ethereum inclusion proof exists for an `InboundMessageDispatched` log with `success = false` — no privileged role, governance action, or malicious-peer assumption is required. Command execution failures on the destination (e.g., out-of-gas) are a normal occurrence in bridge operation, making this readily reachable through ordinary usage.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: on `false`, skip or adjust the reward payment and transition the `PendingOrder` into a distinct failure/retry state (or emit a `MessageDeliveryFailed` event) instead of treating it identically to a successful delivery via `MessageDelivered` and unconditional removal.

## Proof of Concept
1. `do_process_message` enqueues a message and inserts `PendingOrder { nonce, fee, .. }` [7](#0-6) .
2. On Ethereum, the Gateway dispatches the inbound message, but the inner command execution fails, so `InboundMessageDispatched` is emitted with `success = false`.
3. Any signed account submits `submit_delivery_receipt` with a valid proof for this event [4](#0-3) .
4. `process_delivery_receipt` pays `order.fee` to the reward account and removes `PendingOrders[nonce]`, exactly as it would for `success = true` [2](#0-1) .

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L41-50)
```rust
		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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
