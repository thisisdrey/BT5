## Analysis: Excessive indirection analog found in Snowbridge outbound-queue-v2 reward settlement

Reducing the Holdefi report to its core invariant: a payout/state-advance function that indirectly triggers a chain of sub-calls (or that swallows the outcome of a signal it consumes) ends up moving state and paying value that never actually reflects the outcome of the underlying operation. In the Holdefi case, the excess of indirection made it hard to see whether all sub-effects were consistent. The closest verifiable local analog is not "too many function hops" per se, but a chain of calls (`submit_delivery_receipt` → `Verifier::verify` → `DeliveryReceipt::try_from` → `process_delivery_receipt`) in which the decoded `success` field of the underlying Ethereum event is silently dropped, so the pallet advances payout/queue state unconditionally, regardless of whether execution on Ethereum actually succeeded.

### Title
Outbound-queue-v2 `process_delivery_receipt` ignores the `success` flag of the delivery receipt, paying relayer rewards and irreversibly clearing `PendingOrders` for failed deliveries - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`snowbridge_outbound_queue_primitives::v2::DeliveryReceipt` decodes the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event and explicitly carries a `success: bool` field indicating whether the message dispatch on Ethereum succeeded. [1](#0-0) 
However, `Pallet::process_delivery_receipt` never reads or checks `receipt.success` before paying the reward and removing the `PendingOrders` entry. [2](#0-1) 

### Finding Description
The extrinsic `submit_delivery_receipt` verifies the Merkle/receipt proof (that the log genuinely occurred on the bridged chain) and decodes it into a `DeliveryReceipt`, then unconditionally calls `process_delivery_receipt`: [3](#0-2) 

Inside `process_delivery_receipt`, the only checks performed are: (1) the receipt's `gateway` matches the configured `GatewayAddress`, and (2) a `PendingOrder` exists for `receipt.nonce`. If both hold, the relayer reward (`order.fee`) is registered and the `PendingOrders` entry is permanently removed — regardless of the decoded `receipt.success` value: [4](#0-3) 

This breaks the required pivot that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically": the pallet's module doc explicitly documents the intended flow as "Fetch the pending order by nonce ... pay reward with fee attached in the order" and "Remove the order," with no mention of a success gate, confirming this is the actual, not merely accidental, control flow. [5](#0-4) 

A grep across the pallet confirms `success` is never referenced anywhere in `outbound-queue-v2`'s logic, only decoded and dropped.

Because `PendingOrders::<T>::remove(nonce)` executes unconditionally once the pending order is found, this is also irreversible: once a receipt (successful or failed) for a given nonce has been submitted, no subsequent receipt for that nonce can ever be processed again (`InvalidPendingNonce` is returned on retry), so a failed delivery can never be corrected or retried through this path, and the on-chain bookkeeping (`PendingOrders`) permanently and incorrectly reflects the message as "delivered."

### Impact Explanation
- Any relayer can submit a real, correctly-proven `InboundMessageDispatched` event with `success = false` (i.e., the message genuinely failed execution on Ethereum) and still receive the full relayer fee reward via `T::RewardPayment::register_reward`, exactly as if delivery had succeeded — an unbacked/incorrect payout of value that does not correspond to actual completed work.
- The associated `PendingOrder` is deleted regardless of outcome, permanently and incorrectly marking a failed message as settled, with no path to reconcile or retry — a form of duplicate/incorrect settlement of queue state divorced from actual execution outcome, in the message-queue/payout-state category explicitly called out in the pivots.
- This does not require a malicious relayer to forge anything: the underlying proof is real and legitimately verified by `T::Verifier`; the bug is that the pallet does not gate on the semantic outcome (`success`) that the proof itself carries.

### Likelihood Explanation
High: this requires only an unprivileged, honest relayer submitting a genuine (unmodified) failed-delivery receipt — no admin, governance, validator, or malicious-peer assumption is needed. Any Ethereum-side dispatch failure (e.g. reverted command execution) that still emits `InboundMessageDispatched` with `success=false` will trigger this path the moment any relayer calls `submit_delivery_receipt`.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`: pay the reward and remove the `PendingOrder` (current behavior).
- If `false`: do not pay the relayer reward for a failed dispatch (or pay only a smaller "confirmation" fee, consistent with how `pallet-bridge-messages` distinguishes delivery vs. confirmation rewards), and either retain the `PendingOrder` for retry/resolution or transition it into an explicit "failed" state rather than silently deleting it, so the state machine only advances (queue marker cleared, reward paid) after execution success is confirmed, per the message-queue/payout-state invariant.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { nonce, fee, block_number }`.
2. On Ethereum, dispatch of the corresponding inbound message fails (e.g. due to insufficient gas or a reverting command), and the Gateway contract still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer captures this genuine event/receipt and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the event genuinely happened), `DeliveryReceipt::try_from` decodes `success = false` correctly.
5. `process_delivery_receipt` checks only `gateway` and existence of `PendingOrders[nonce]`; since both pass, it calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and then `PendingOrders::<T>::remove(nonce)`, exactly as in the passing "success" test case: [6](#0-5) 
6. The relayer is paid despite the failed delivery, and the failed message is permanently marked as resolved in `PendingOrders`, with no mechanism to retry or reconcile it.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L421-448)
```rust
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
