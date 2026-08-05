Audit Report

## Title
Outbound Queue V2 settles pending orders and pays relayer rewards without checking on-chain execution success - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`Pallet::process_delivery_receipt` in `pallet-outbound-queue-v2` pays the relayer reward and removes the `PendingOrder` for a message purely on the basis of a verified `InboundMessageDispatched` event log, without ever inspecting the `success: bool` field carried in that same decoded `DeliveryReceipt`. This conflates "the dispatch attempt was broadcast/verified" with "the message actually executed successfully on Ethereum," allowing settlement and payout to proceed for messages whose Ethereum-side commands reverted.

## Finding Description
`DeliveryReceipt` is decoded from the Gateway's `InboundMessageDispatched(nonce, topic, success, reward_address)` event and explicitly carries a `success` field: [1](#0-0) 

`Pallet::process_delivery_receipt` fetches the `order` by nonce, unconditionally pays `order.fee` via `T::RewardPayment::register_reward` when `order.fee > 0`, removes the order from `PendingOrders`, and emits `Event::MessageDelivered` — none of these steps read `receipt.success`: [2](#0-1) 

The extrinsic wrapper `submit_delivery_receipt` only verifies that the event log is authentic (`T::Verifier::verify`) and decodable (`DeliveryReceipt::try_from`), then forwards straight into `process_delivery_receipt`: [3](#0-2) . Neither of these checks constrains `success`. A grep of the whole `bridges/snowbridge` tree confirms `receipt.success` (or any use of the `success` field after decoding) is referenced nowhere except its own struct definition, i.e., it is decoded but never consulted anywhere in the settlement path.

The pallet's own doc comment documents the intended flow as paying the reward and removing the order "when the message has been verified and **executed**," which implies successful execution should gate settlement — the implementation does not enforce this: [4](#0-3) 

## Impact Explanation
Because settlement and reward payout advance solely on log authenticity rather than on the encoded execution outcome, a message whose Ethereum-side command reverts (`success == false`) is treated identically to a genuinely delivered-and-executed message: the relayer is paid `order.fee`, the `PendingOrder` is permanently removed (no retry path), and `MessageDelivered` is emitted as if the cross-chain operation completed. This breaks the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." The result is a duplicate/incorrect settlement and an unbacked payout: the relayer reward is disbursed and the order permanently cleared even though the intended Ethereum-side state transition never took effect, and there is no mechanism to retry or refund the affected message — a permanent loss of the intended bridge effect while still consuming reward funds.

## Likelihood Explanation
`submit_delivery_receipt` is a public, unprivileged extrinsic gated only by `ensure_signed` — any relayer (not a privileged or trusted role) can call it: [5](#0-4) . Any legitimate on-chain event where a dispatched command reverts on Ethereum (e.g., due to insufficient gas supplied by the relayer, a paused contract, or any other legitimately-occurring revert condition) produces a real, verifiable `InboundMessageDispatched` log with `success = false`. No compromise of the verifier, prover, or governance is required — an ordinary relayer submitting a truthful, verifiable receipt for a failed dispatch is sufficient to trigger full settlement and payment.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before finalizing settlement: only pay `order.fee` and remove the `PendingOrder` when `success == true`. On `success == false`, avoid paying the full reward (or pay a reduced/no reward), and transition the order to a distinct "failed" state (or leave it pending for retry/refund) rather than silently removing it, so failed dispatches remain observable and recoverable instead of being conflated with successful delivery.

## Proof of Concept
1. `do_process_message` queues a message and inserts `PendingOrders::<T>::insert(nonce, order)` with `order.fee > 0`.
2. On Ethereum, the Gateway processes the message but the embedded command reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid finalized-header + receipt proof for this log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the log is genuine); `DeliveryReceipt::try_from` decodes `success: false` into `receipt.success`.
5. `process_delivery_receipt` runs: it pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward`, calls `<PendingOrders<T>>::remove(nonce)`, and emits `Event::MessageDelivered { nonce }` — identical to the success path exercised in `submit_delivery_receipt_succeeds_after_unhalt`, whose assertions never vary based on the `success` flag: [6](#0-5) . A unit test asserting `PendingOrders::<Test>::get(nonce).is_none()` and a nonzero reward payout after submitting a receipt with `success: false` would demonstrate the bug directly.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L418-448)
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
```
