Confirmed: `submit_delivery_receipt` at [1](#0-0)  decodes the `DeliveryReceipt` (which carries a `success` field per [2](#0-1) ) and passes it straight into `process_delivery_receipt` without ever branching on `receipt.success`.

### Title
Relayer reward paid and pending order settled regardless of on-chain delivery outcome (`receipt.success` never checked) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is: a boolean gating flag that should control whether a downstream action is performed (`_shouldSendToL1`) is silently hard-coded/ignored, so the intended conditional side-effect (publishing bytecode to L1) never happens even when it should. The local analog inverts this pattern in `pallet_outbound_queue_v2`: the `DeliveryReceipt::success` field, which is the on-chain-verified proof of whether the Ethereum-side command execution actually succeeded, is decoded from the verified event log but is never read or branched upon in `process_delivery_receipt`. The pending order is unconditionally removed and the relayer reward is unconditionally paid based only on the existence of `PendingOrders[nonce]`, not on whether `receipt.success` is `true`.

### Finding Description
`submit_delivery_receipt` verifies the Merkle/receipt proof via `T::Verifier::verify` and decodes the `InboundMessageDispatched` event into a `DeliveryReceipt { gateway, nonce, topic, success, reward_address }` [3](#0-2) . This proves that Ethereum's Gateway contract emitted an event with a specific `success` flag for a specific `nonce`.

`process_delivery_receipt` then does:
```rust
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
``` [4](#0-3) 

`receipt.success` is decoded and available (it is part of the verified proof, exactly analogous to the `_shouldSendToL1` flag that was computed but never checked before the report was fixed), yet the pallet pays the reward and closes the order purely because a `PendingOrder` exists for that `nonce`, irrespective of whether the message actually executed successfully on Ethereum. There is no `ensure!(receipt.success, ...)` or any branch distinguishing failed dispatch. The existing "guard" — Gateway address equality and the delivery proof verification — only proves that *some* event with that nonce was emitted, not that the command batch executed without reverting on the destination.

### Impact Explanation
Any relayer can permissionlessly call `submit_delivery_receipt` for a message whose execution genuinely failed on Ethereum (`success: false`) — for example when downstream commands revert due to insufficient gas, an invalid recipient, or an unwind — and still receive the full relayer fee stored in `PendingOrders`, while the order is removed from tracking as if delivery had definitively succeeded. This breaks the "duplicate settlement or payout" / "public underpriced work" invariant: rewards intended only for genuinely successful relaying are paid for failed relaying, and the order-tracking state (`PendingOrders`) is advanced/cleared without the intended condition (successful execution) being met, matching the pivot requirement that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

### Likelihood Explanation
The bug is triggerable by any unprivileged, non-malicious relayer submitting an entirely legitimate delivery proof for a message that failed on the Ethereum side (a routine and expected occurrence, not requiring a malicious actor, prover, or validator). No governance or privileged action is needed — `submit_delivery_receipt` is a public, permissionless extrinsic [5](#0-4) .

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; for `success == false`, still remove/settle the `PendingOrder` (to prevent indefinite unclaimed order buildup) but emit a distinct event (e.g. `MessageDeliveryFailed`) and skip reward payment, or apply an explicit penalty/partial-fee policy instead of full payment.

### Proof of Concept
1. A message is sent through `do_process_message`, creating `PendingOrders[nonce] = PendingOrder { fee: F, .. }` [6](#0-5) .
2. On Ethereum, the Gateway executes the commands for that nonce but they revert/fail, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer (not malicious, just doing routine relaying) submits this genuine event log + proof via `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the log is real), `DeliveryReceipt::try_from` decodes `success=false` correctly, but `process_delivery_receipt` never inspects it: `order.fee > 0` is true, so `T::RewardPayment::register_reward` pays the full fee, and `PendingOrders::remove(nonce)` clears the order, exactly as if delivery had succeeded — as seen in the existing test `submit_delivery_receipt_succeeds_after_unhalt`, which never varies `success` and still results in reward payment [7](#0-6) .

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L293-317)
```rust
	#[pallet::call]
	impl<T: Config> Pallet<T>
	where
		<T as frame_system::Config>::AccountId: From<[u8; 32]>,
	{
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-438)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L464-479)
```rust
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
```

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L420-449)
```rust
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
