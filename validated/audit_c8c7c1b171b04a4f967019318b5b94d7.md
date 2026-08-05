Confirmed: `receipt.success` is decoded but never referenced anywhere else in the codebase outside the struct definition itself, and `MessageDelivered` is only emitted, never consumed elsewhere with a success check.Audit Report

## Title
`process_delivery_receipt` in Snowbridge outbound-queue-v2 pays relayer rewards and permanently settles `PendingOrder`s without checking the `success` field of the delivered message - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`DeliveryReceipt` decoded from the Ethereum Gateway's `InboundMessageDispatched` event carries a `success: bool` field that is the only on-chain signal distinguishing a failed dispatch from a successful one, but `Pallet::process_delivery_receipt` never reads it before paying the relayer reward and removing the `PendingOrder`. This causes messages whose commands revert on Ethereum to be settled and rewarded identically to genuinely successful deliveries.

## Finding Description
`DeliveryReceipt` is decoded with an explicit `success` field sourced directly from the Gateway's `InboundMessageDispatched(nonce, topic, success, reward_address)` event log: [1](#0-0) 

`submit_delivery_receipt` is a public, unprivileged extrinsic gated only by `ensure_signed`, which verifies the Ethereum proof, decodes the `DeliveryReceipt`, and hands it to `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` fetches the `PendingOrder` by nonce, unconditionally pays `order.fee` to the reward account via `T::RewardPayment::register_reward`, removes the order from `PendingOrders`, and emits `Event::MessageDelivered` — at no point does it inspect `receipt.success`: [3](#0-2) 

The pallet's own doc-comment describes the intended flow as occurring "When the message has been verified and executed," implying settlement should track actual successful execution, not just a broadcast attempt: [4](#0-3) 

A repository-wide search confirms `receipt.success` (and the `success` field generally) is never referenced anywhere outside its own struct definition, and `MessageDelivered` has no downstream consumer that separately checks execution outcome — the pallet-level settlement logic is the sole and final arbiter, and it discards the signal entirely.

## Impact Explanation
Any dispatched message whose commands revert or fail on the Ethereum Gateway (`success == false`) is nonetheless treated as fully and successfully delivered on BridgeHub: the relayer reward (`order.fee`) is paid in full via `T::RewardPayment::register_reward`, the `PendingOrder` is irrevocably removed from `PendingOrders`, and `Event::MessageDelivered` is emitted as if execution succeeded. This is an incorrect/unbacked payout — reward funds are disbursed for work that did not achieve its intended on-chain effect on Ethereum — and it also permanently discards the only state (`PendingOrder`) that could support a retry or failure-tracking flow, since the mapping is unconditionally removed regardless of `success`. This matches the required impact category of incorrect settlement/payout not conditioned on actual successful execution.

## Likelihood Explanation
The exploit path requires nothing beyond ordinary, permitted relayer action: `submit_delivery_receipt` only requires `ensure_signed` (any signed account) and a genuine, valid Ethereum proof for an `InboundMessageDispatched` log — which the Gateway emits regardless of whether the dispatched commands succeeded or reverted. Any relayer whose message legitimately reverts on Ethereum (e.g., insufficient gas, a reverting command, or any other execution failure) can submit that failure receipt and still be paid the full reward and have the order settled, with no attacker-specific privilege or malicious infrastructure required.

## Recommendation
Check `receipt.success` in `process_delivery_receipt` before finalizing settlement. On `success == false`, do not pay the full relayer reward (or pay a reduced/no reward), and either keep the `PendingOrder` in a distinct failed state for observability/retry or handle it through an explicit failure path, rather than removing it identically to the success case. Only treat "delivered" (`MessageDelivered`) as equivalent to "executed successfully" when `receipt.success == true`.

## Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders::<T>::insert(nonce, order)` with `order.fee > 0`.
2. On Ethereum, the Gateway processes the message but the embedded command reverts, so the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds the finalized-header + receipt proof for this log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the log genuinely exists on-chain), and `DeliveryReceipt::try_from` decodes `success: false`.
5. `process_delivery_receipt` runs: since it never reads `receipt.success`, it pays `order.fee` to the reward account via `T::RewardPayment::register_reward`, calls `PendingOrders::<T>::remove(nonce)`, and emits `Event::MessageDelivered { nonce }` — behavior identical to a genuine successful delivery, confirmable by comparing against the existing test `submit_delivery_receipt_succeeds_after_unhalt`, whose assertions never vary based on the `success` flag: [5](#0-4)

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
