Audit Report

## Title
Relayer reward and order settlement in `process_delivery_receipt` ignore the `success` flag of the delivery receipt - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt::try_from` decodes the Ethereum `InboundMessageDispatched` event's `success: bool` field into the `DeliveryReceipt` struct [1](#0-0) , but `Pallet::process_delivery_receipt` never reads `receipt.success` before paying the relayer reward and removing the `PendingOrder` [2](#0-1) . This causes settlement (reward payout + order removal + `MessageDelivered` event) to proceed identically whether the corresponding command succeeded or reverted on Ethereum.

## Finding Description
The module doc explicitly frames the intended flow as: once a message has been "verified and executed" on Ethereum, the relayer submits a receipt to pay the reward and remove the pending order [3](#0-2) . The `submit_delivery_receipt` extrinsic is unauthenticated beyond `ensure_signed`, verifies the Merkle/receipt proof via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt`, and forwards it to `process_delivery_receipt` [4](#0-3) .

Inside `process_delivery_receipt`, only `receipt.gateway` (must match `T::GatewayAddress`) and existence of a `PendingOrders[nonce]` entry are checked; `receipt.success` is decoded into the struct but never inspected before the reward is registered via `T::RewardPayment::register_reward` and the order is unconditionally removed with `MessageDelivered` emitted [5](#0-4) . This is confirmed to be the only site in the pallet that consumes the `DeliveryReceipt` value — no other check on `success` exists anywhere in the pallet.

Because the Gateway contract emits `InboundMessageDispatched(nonce, topic, success, reward_address)` regardless of whether the dispatched command actually executed successfully (e.g. it reverted due to insufficient gas or business-logic failure), a proof of this event log with `success = false` is just as valid and verifiable as one with `success = true`. The pallet's existing guards (`InvalidGateway`, `InvalidPendingNonce`, `Verification`) only validate proof authenticity and route/nonce binding — none of them validate the execution outcome that `success` is meant to convey.

## Impact Explanation
This violates the settlement invariant that reward payout and bridge-state markers (`PendingOrders` removal, `MessageDelivered` event) must only advance after the corresponding cross-chain execution actually succeeds. A relayer is paid the full order fee, and BridgeHub state considers the message "delivered," even when the dispatched command reverted on Ethereum. This misaligns bridge-side bookkeeping from actual cross-chain effects and removes any on-chain signal for retry/compensation of failed commands — a case of settlement finalizing/paying out for un-executed cross-chain effects, i.e., an unbacked/incorrect relayer payout tied to a corrupted acceptance of the `success` field in the `DeliveryReceipt`.

## Likelihood Explanation
Exploitation requires only an unprivileged party to relay a legitimate, verifiable Ethereum event log (`InboundMessageDispatched` with `success=false`) via the public `submit_delivery_receipt` extrinsic, which any signed account can call [4](#0-3) . No malicious relayer, validator, governance, or compromised-key assumption is needed — a `success=false` event is emitted by the trusted Gateway contract itself whenever a dispatched command genuinely fails on Ethereum (e.g., out-of-gas or reverted XCM `Transact`), making this reachable under normal, expected bridge operating conditions.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only pay `order.fee` to the relayer and remove `PendingOrders[nonce]` with `MessageDelivered` when `success == true`. On `success == false`, emit a distinct event (e.g. `MessageDispatchFailed`) and apply an explicit failure policy (e.g., remove the order without reward, or route to a retry/compensation path) so settlement state advances in lock-step with the confirmed execution outcome.

## Proof of Concept
1. `do_process_message` enqueues a message with `fee > 0`, creating `PendingOrders[nonce]` [6](#0-5) .
2. The message is relayed to Ethereum; the dispatched command reverts, and the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any signed account obtains proof of this log and calls `submit_delivery_receipt` [4](#0-3) .
4. `process_delivery_receipt` verifies only `gateway` and `nonce`, pays the full `order.fee` reward, removes `PendingOrders[nonce]`, and emits `MessageDelivered` — identical to the success case, despite the underlying command failing on Ethereum [5](#0-4) .

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-440)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });
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
