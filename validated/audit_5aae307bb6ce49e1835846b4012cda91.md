Based on my research, I found a genuine local analog to the "premature settlement/state classification" bug class in the Snowbridge V2 outbound queue's delivery-receipt handling.

### Title
Outbound Queue V2 settles pending orders and pays relayer rewards without checking on-chain execution success - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The Zeta report's core broken invariant is: a cross-chain transfer is classified as "settled" (removed from pending/in-transit tracking) based on an event that only proves an attempt was broadcast, not that the corresponding state transition actually completed with the intended outcome. The Snowbridge `pallet-outbound-queue-v2` exhibits the same class of defect: `process_delivery_receipt` advances settlement state (removes the `PendingOrder`, emits `MessageDelivered`, pays the relayer reward) purely because a verified `InboundMessageDispatched` event log was produced, while completely ignoring the `success` field of that same event, which is the only signal distinguishing a failed dispatch from a successful one.

### Finding Description
`DeliveryReceipt` decoded from the Ethereum Gateway's `InboundMessageDispatched` event explicitly carries a `success: bool` field: [1](#0-0) 

`Pallet::process_delivery_receipt` reads the `order`, unconditionally pays the reward tied to `order.fee`, then removes the order and emits `MessageDelivered` — at no point does it inspect `receipt.success`: [2](#0-1) 

The doc-comment for the pallet explicitly documents the intended flow as "When the message has been verified and executed, the relayer will call... to... pay reward with fee attached in the order... Remove the order," implying settlement should reflect actual, successful execution outcome, yet the code path treats a *failed* dispatch (`success == false`) identically to a *successful* one: [3](#0-2) 

This mirrors the Zeta analog precisely: the existence of a downstream event (broadcast to `OutTxTracker` / `InboundMessageDispatched` log) is used as the trigger to finalize accounting/settlement state, without validating that the underlying operation actually achieved its intended effect (mined+applied state transition / successful command execution).

### Impact Explanation
Because `success` is unused, a message whose commands revert or fail on the Ethereum Gateway is still recorded via `Event::MessageDelivered`, its `PendingOrder` is removed, and the relayer is paid in full as if delivery had succeeded. Any downstream logic, monitoring, or accounting (both off-chain relayer-reward tooling and on-chain consumers of `MessageDelivered`) that treats this event as proof of successful cross-chain execution will be misled, exactly like the Zeta supply checker being misled by premature "settled" classification. This can also mask real failures on the bridge (silent loss of intended Ethereum-side effects) while still consuming reward funds as if the operation succeeded.

### Likelihood Explanation
Any relayer that watches the Gateway for `InboundMessageDispatched` events (including their own or third-party's forced-failure calls where relayer supplies too little gas or a command legitimately reverts) can submit a valid Ethereum-side proof with `success = false` and still trigger full settlement and reward payment through `submit_delivery_receipt`, which is a public, unprivileged extrinsic requiring only `ensure_signed`: [4](#0-3) 
No governance, admin, or malicious-relayer assumption is needed beyond the relayer performing a permitted, unprivileged action — the path is straightforwardly reachable by anyone who can produce a valid Ethereum receipt proof for a message nonce with a pending order.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before finalizing settlement: on `success == false`, either withhold/replace the reward, keep or transition the `PendingOrder` to a distinct "failed" state for observability/retry, and only pay a full relaying reward and remove the order when `success == true`, so that "delivered" and "executed successfully" are not conflated.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders::<T>::insert(nonce, order)` with a nonzero fee.
2. On Ethereum, the Gateway processes the message but the embedded command reverts/fails, so the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds the finalized-header + receipt proof for this log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the log genuinely exists), `DeliveryReceipt::try_from` decodes `success: false`.
5. `process_delivery_receipt` is invoked; it never reads `receipt.success`, so it pays `order.fee` to the reward account, removes `PendingOrders::<T>::remove(nonce)`, and emits `Event::MessageDelivered { nonce }` — identical behavior to the success case shown in the existing test `submit_delivery_receipt_succeeds_after_unhalt`, which never varies its assertions based on the `success` flag: [5](#0-4) 

**Uncertainty note:** I could not fully verify within the available searches whether higher-level runtime or off-chain relayer tooling elsewhere in the repo separately checks `receipt.success` before acting on `MessageDelivered` (e.g., to gate a retry or refund flow), which would partially mitigate the practical impact. The pallet-level logic itself, however, is confirmed to ignore this field.

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
