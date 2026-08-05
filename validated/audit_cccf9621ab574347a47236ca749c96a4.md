Audit Report

## Title
Snowbridge outbound-queue-v2 pays relayer reward from `PendingOrders` regardless of Ethereum delivery outcome, ignoring the decoded `success` field - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`Pallet::process_delivery_receipt` pays out the full relayer reward attached to a `PendingOrder` as soon as a valid Ethereum receipt proof is supplied for the matching `nonce`, without ever inspecting the `success` field of the decoded `DeliveryReceipt`. This allows a relayer to collect the full fee for a message whose execution on the Ethereum Gateway contract actually failed.

## Finding Description
`submit_delivery_receipt` verifies the receipt proof of a real Ethereum log via `T::Verifier::verify`, then decodes it into a `DeliveryReceipt` that carries a `success: bool` field sourced directly from the on-chain `InboundMessageDispatched` event [1](#0-0) , and dispatches to `process_delivery_receipt` [2](#0-1) .

`process_delivery_receipt` only checks `receipt.gateway` against `T::GatewayAddress`, looks up the `PendingOrder` by `receipt.nonce`, and unconditionally pays `order.fee` via `T::RewardPayment::register_reward` before removing the order — `receipt.success` and `receipt.topic` are never read anywhere in the function body [3](#0-2) . The `DeliveryReceipt::try_from` decode path faithfully carries the `success` field from the ABI-decoded event log [4](#0-3)  but this value is silently discarded by the caller.

The Merkle/receipt proof only binds `gateway` and implicitly the log content used for decoding `nonce`/`topic`/`success`/`reward_address`; the pallet's own logic does not gate the reward payment on the decoded `success` outcome, so any genuine `InboundMessageDispatched` event — whether the underlying XCM `Transact`/message execution succeeded or reverted on Ethereum — results in identical payout behavior. The pallet's doc comment describes the intended flow as "fetch the pending order by nonce ... pay reward with fee attached in the order" with no conditional on delivery success [5](#0-4) , confirming this is the actual designed (but flawed) behavior, not an isolated oversight.

## Impact Explanation
This violates the requirement that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A relayer is paid the full `order.fee` reward for messages that failed to execute on the Ethereum Gateway, causing unbacked settlement of bridge reward funds out of the reward pot for work that did not successfully complete. Additionally, since `PendingOrders::remove(nonce)` happens unconditionally, the on-chain record that the message needs redelivery/retry/refund handling is permanently destroyed regardless of the `success` outcome, compounding the fund-loss/bridge-state issue with a loss of retry-ability.

## Likelihood Explanation
`submit_delivery_receipt` is a public extrinsic gated only by `ensure_signed` [6](#0-5) , so any signed account can invoke it. The attacker does not need to forge any proof — they simply need a genuine, provable Ethereum `InboundMessageDispatched(success=false)` event, which occurs naturally whenever a Gateway-side dispatched command reverts (e.g., an underfunded/failing `Transact` payload), a routine and reasonably likely occurrence in bridge operation. No governance, validator collusion, or privileged access is required, making this a highly reachable, repeatable path for any relayer.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`. On failure, still resolve the `PendingOrder` state (remove it or move it to a dedicated failure/retry-tracking path) but withhold the fee payout, and emit a distinct event (e.g., `MessageDeliveryFailed`) so downstream systems and relayers can react and potentially resubmit or refund.

## Proof of Concept
1. `do_process_message` creates a `PendingOrder { nonce, fee, .. }` in `PendingOrders` for a queued outbound message [7](#0-6) .
2. The message is relayed to Ethereum, but its dispatched command reverts on the Gateway, so the genuine `InboundMessageDispatched` event on Ethereum carries `success = false`.
3. Any signed account submits `submit_delivery_receipt` with a valid receipt proof of this real event; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success = false` correctly.
4. `process_delivery_receipt` ignores `receipt.success`, pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward`, and removes the `PendingOrder` — confirmed by reading lines 445-480 of `lib.rs`, where no branch on `receipt.success` exists.
5. Result: reward funds are paid out for a failed delivery and the accounting record of the pending redelivery need is permanently erased, matching the "duplicate settlement or payout" / bridge-state impact category.

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
