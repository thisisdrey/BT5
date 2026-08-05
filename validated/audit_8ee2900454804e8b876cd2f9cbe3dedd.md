This confirms the claim exactly as described. The code matches the citation verbatim.This confirms the claim. `success` is decoded into `DeliveryReceipt` at [1](#0-0) [2](#0-1)  but `process_delivery_receipt` never reads `receipt.success`, unconditionally paying the reward and removing the order at [3](#0-2) . The doc-comment confirms the intended flow omits any success check [4](#0-3) . No other file in the codebase consumes `receipt.success` before this call.

Audit Report

## Title
`process_delivery_receipt` pays relayer reward without checking `DeliveryReceipt.success`, allowing payout for failed message delivery - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`snowbridge_pallet_outbound_queue_v2::Pallet::process_delivery_receipt` decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` struct containing a `success: bool` field, but never reads or checks that field before paying the relayer's reward and removing the pending order. Any legitimately-included Ethereum event with `success = false` (a message that failed on the Gateway contract) still results in the relayer being paid and the order being deleted, identical to a successful delivery.

## Finding Description
`DeliveryReceipt` is decoded from the Ethereum `InboundMessageDispatched` log with an explicit `success` field faithfully carried from the on-chain event (`bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs`, lines 14-27 and 35-51). In `process_delivery_receipt` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`, lines 445-480), the function checks only `receipt.gateway` against `T::GatewayAddress::get()` and looks up `PendingOrders` by `receipt.nonce`; it then unconditionally calls `T::RewardPayment::register_reward` (guarded only by `order.fee > 0`) and removes the order via `<PendingOrders<T>>::remove(nonce)`. The `receipt.success` field is never referenced anywhere in this path. The `Verifier::verify` implementation used by the extrinsic only proves inclusion/finality of the log in a block — it does not and cannot validate the semantic contents (the `success` flag) of that event. Since `success = false` is a genuine, provable state that Ethereum's Gateway contract can legitimately emit for a message that reverted/failed execution, no forged proof is required to trigger this path.

## Impact Explanation
This breaks the intended invariant, stated in the pallet's own module doc, that reward payout should occur only once delivery is confirmed successful ("pay reward with fee attached in the order" after successful execution). Because the check is absent, BridgeHub pays the relayer's fee from `PendingOrders` and irreversibly removes the order regardless of whether the corresponding Ethereum-side execution actually succeeded — this is a duplicate/incorrect settlement matching the "duplicate settlement or payout" and "message queues/bridge markers/receipts/payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivots in the impact gate. There is no path to retry or correct after the order is removed.

## Likelihood Explanation
High and requires no privileged, malicious, or off-repo actor. Any unprivileged relayer can call the public extrinsic `submit_delivery_receipt` with a real, verifiable Ethereum receipt proof for a message that failed execution on the Gateway contract — a normal occurrence (e.g. insufficient gas, downstream XCM dispatch failure), not an attack precondition. Existing checks (`Verifier::verify`, halted-state check, receipt inclusion, tx index) are all satisfied by a genuine failed-delivery event and do none of them inspect the `success` field.

## Recommendation
Add an explicit check on `receipt.success` in `process_delivery_receipt` before paying the reward, e.g. `ensure!(receipt.success, Error::<T>::DeliveryFailed);`, placed before `T::RewardPayment::register_reward` is invoked, so reward payout and `PendingOrders` removal occur only for successful deliveries. Consider a distinct handling path for failed deliveries (retry, refund of `fee`, or a dedicated failure event) instead of discarding the signal.

## Proof of Concept
1. `PendingOrders` holds an order for `nonce = N`, `fee = F`.
2. The message is delivered to the Ethereum Gateway but execution fails, so the Gateway emits `InboundMessageDispatched(nonce = N, topic, success = false, reward_address)`.
3. A relayer builds a valid receipt-inclusion proof for this real event (as in `submit_delivery_receipt_succeeds_after_unhalt`, `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` lines 420-449) and calls `submit_delivery_receipt(origin, event)`.
4. `Verifier::verify` succeeds, `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` ignores it, pays `order.fee` to `reward_account`, and removes the order — identical outcome to a successful delivery.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
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
