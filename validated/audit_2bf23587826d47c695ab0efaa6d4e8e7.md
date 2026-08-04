### Title
Relayer reward paid and delivery order settled without checking the on-chain `success` flag in the Ethereum delivery receipt - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` (called from the public extrinsic `submit_delivery_receipt`) decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` that carries an explicit `success: bool` field, but never inspects that field before rewarding the relayer and clearing the pending order. This is the same bug class as the reported `_swapTokenExactOutput`: a call that can indicate failure (here, `success == false`) is treated identically to success by the caller, so the transaction succeeds and advances state even though the real-world outcome was a failed delivery.

### Finding Description
The delivery-receipt type explicitly models delivery success/failure: [1](#0-0) 

The extrinsic entry point verifies the Ethereum proof and decodes the receipt, then unconditionally forwards it to `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` validates the gateway address, resolves the reward account, looks up the `PendingOrder` by nonce, pays out `order.fee` via `T::RewardPayment::register_reward`, removes the order from `PendingOrders`, and emits `MessageDelivered` — all without ever reading `receipt.success`: [3](#0-2) 

The only guards present are `ensure!(T::GatewayAddress::get() == receipt.gateway, ...)` and `PendingOrders::<T>::get(nonce).ok_or(...)`. Neither of these checks the delivery outcome. Because `success` is decoded straight from the Ethereum log by `DeliveryReceipt::try_from`, an honest, non-malicious relayer submitting a legitimate proof for a `false`-success log (i.e., the message dispatch reverted on Ethereum, but the event was still emitted and logged) will still cause:
- the relayer to be rewarded for a failed delivery, and
- the `PendingOrder` to be permanently removed, meaning it can never be retried, reprocessed, or re-rewarded correctly.

This mirrors exactly the reported bug pattern: a boolean/return-code that signals failure exists, but the caller does not branch on it, so success-path state transitions (fund transfer / order settlement) happen regardless.

### Impact Explanation
- Reward funds are paid out (`register_reward`) for message deliveries that Ethereum itself reports as failed — an unbacked/incorrect payout, matching the "theft or unbacked mint or unlock" and "duplicate settlement or payout" impact categories.
- The `PendingOrder` is deleted unconditionally on any receipt with a matching nonce and correct gateway, regardless of `success`. Since orders are removed, there is no path to reconcile/re-settle failed deliveries; the queue's settlement state has advanced ("PendingOrders" marker) without a corresponding successful execution, violating the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot.
- This can be triggered by any signed account submitting a genuine (non-forged) proof of a real Ethereum event with `success = false` — no malicious relayer, prover, or validator behavior is required, only correct usage of the public extrinsic against real chain data.

### Likelihood Explanation
High under normal operating conditions: on Ethereum, a dispatched inbound message can legitimately fail (e.g. insufficient gas, XCM execution revert on the gateway) and the `InboundMessageDispatched` event is still emitted with `success = false`. Any relayer who submits this real event via `submit_delivery_receipt` will pass proof verification (the event genuinely happened) and hit this code path, since nothing in the pallet currently distinguishes success from failure.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` and remove the `PendingOrder` when `success == true`. On `success == false`, emit a distinct failure event (e.g. `MessageDeliveryFailed`) and decide an explicit policy for the order (e.g., keep it pending for a compensating action/refund path, or move it to a "failed" state) rather than silently deleting it and paying the relayer as if it succeeded.

### Proof of Concept
1. A message is queued via `SendMessage::deliver`, producing a `PendingOrder { nonce, fee, .. }` in `PendingOrders`.
2. On Ethereum, the gateway contract attempts to dispatch this message but the dispatch reverts/fails; the contract still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid execution/receipt proof for this real event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the proof is genuine), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` proceeds: `order.fee > 0` triggers `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, `PendingOrders::<T>::remove(nonce)` executes, and `Event::MessageDelivered { nonce }` is emitted — identical to the success case in existing tests such as: [4](#0-3) 
   The relayer is rewarded and the order is irrecoverably cleared despite the underlying Ethereum dispatch having failed.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L418-449)
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
}
```
