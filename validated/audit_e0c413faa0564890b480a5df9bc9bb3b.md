This confirms the claim exactly: `success` is decoded in `DeliveryReceipt` [1](#0-0)  but never referenced anywhere in the outbound-queue-v2 pallet logic — a grep across the pallet for `success` returns no matches. `process_delivery_receipt` only checks `receipt.gateway` and the existence of a `PendingOrder` for `receipt.nonce`, then unconditionally pays the reward and removes the order [2](#0-1) . This matches the documented intended flow that settlement should occur "when the message has been verified and executed" [3](#0-2) , yet the code never gates on the execution outcome. The extrinsic entry point is public and only requires `ensure_signed`, with no additional authorization on the `success` semantics [4](#0-3) .

Audit Report

## Title
Relayer reward and order settlement in `process_delivery_receipt` ignore the `success` flag of the delivery receipt - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt` carries a `success: bool` field reflecting whether the dispatched command actually executed successfully on Ethereum, decoded from the Gateway contract's `InboundMessageDispatched` event log. `Pallet::process_delivery_receipt` verifies the proof, checks only the gateway address and nonce existence, then unconditionally pays the relayer reward from `order.fee` and removes the `PendingOrder`, without ever branching on `receipt.success`.

## Finding Description
The pallet's doc comment describes the intended flow: after a message has been "verified and executed" on Ethereum, the relayer submits the receipt to "pay reward with fee attached in the order" and remove the order. In the actual implementation, `process_delivery_receipt` performs:
```rust
ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
...
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
```
The `receipt.success` field, decoded in `DeliveryReceipt::try_from` from the Gateway's `InboundMessageDispatched(nonce, topic, success, reward_address)` event, is never read in the pallet. A repo-wide grep confirms `success` appears in the primitives crate's decode logic only, with zero references anywhere in `outbound-queue-v2`. This means the settlement of `PendingOrders` (reward payout + storage removal + `MessageDelivered` event) proceeds identically whether the corresponding command execution on Ethereum succeeded or reverted.

## Impact Explanation
This violates the intended one-to-one binding between "message executed successfully on Ethereum" and "reward paid / order settled on BridgeHub." A command that reverts on the Ethereum side (Gateway emits `success = false`) is still treated as fully delivered on the Substrate side: the relayer is paid the full fee, `PendingOrders[nonce]` is removed, and `MessageDelivered` is emitted with no distinguishing signal for downstream logic. This corrupts the settlement state (the `PendingOrders` map entry and payout state) so that it advances even though execution/dispatch did not succeed, matching the "duplicate settlement or payout" / "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot.

## Likelihood Explanation
Any unprivileged, honest relayer submitting a legitimate Ethereum event log with `success = false` triggers this path — no malicious actor is required, since `success` is set by the trusted Gateway contract itself whenever a dispatched command reverts or runs out of gas. `submit_delivery_receipt` is a public extrinsic gated only by `ensure_signed`, with no check tying settlement finality to the execution outcome.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only pay the reward and remove the `PendingOrder` (with `MessageDelivered`) when `success == true`; on `success == false`, emit a distinct event (e.g., `MessageDispatchFailed`) and apply an explicit failure policy (e.g., withhold or reduce reward, and/or route the order to a retry/compensation path) instead of settling identically to the success case.

## Proof of Concept
1. `do_process_message` enqueues a message and creates `PendingOrders[nonce]` with `fee > 0`.
2. The message is relayed to Ethereum; dispatch reverts, and the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid proof for this log and calls `submit_delivery_receipt`.
4. `process_delivery_receipt` verifies gateway and nonce only, pays the full `order.fee` reward, removes `PendingOrders[nonce]`, and emits `MessageDelivered` — identical outcome to the success case despite the underlying command having failed on Ethereum.

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
