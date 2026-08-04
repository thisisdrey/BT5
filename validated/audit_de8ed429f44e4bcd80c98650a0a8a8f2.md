### Title
`process_delivery_receipt` pays relayer reward and settles the order regardless of on-chain delivery `success` status - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries an explicit `success: bool` field [1](#0-0) , but `Pallet::process_delivery_receipt` never inspects it before paying the relayer reward and permanently removing the `PendingOrder`. This mirrors the external report's core defect class: one code path (the documented/intended "success" semantics) is silently dropped in the actual settlement logic, so a state transition (payout + order clearance) happens unconditionally instead of being gated on the correct condition.

### Finding Description
`submit_delivery_receipt` verifies the Merkle/BEEFY proof of the Ethereum event log and decodes it into a `DeliveryReceipt`, then calls `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` only checks the gateway address and looks up the `PendingOrder` by nonce; it pays the fee reward whenever `order.fee > 0` and then unconditionally removes the order — the `receipt.success` field is never read: [3](#0-2) 

The decoded `DeliveryReceipt` struct explicitly carries `success: bool` as "Delivery status", implying it is meant to distinguish a successfully executed message from a reverted/failed one on the Ethereum side: [1](#0-0) 

Because the pallet ignores this field, any valid, correctly-proven `InboundMessageDispatched` event — even one where `success == false` (i.e., the message execution reverted on Ethereum) — will still:
1. Pay the relayer (or an attacker-controlled `reward_address`) the full `order.fee`.
2. Permanently remove the `PendingOrder` from `PendingOrders`, making the order un-retriable/un-reclaimable.

This is a public, unprivileged path: any relayer can call `submit_delivery_receipt` with a legitimately proven but `success:false` event log for a message whose execution failed on the destination chain, and still be rewarded as if delivery succeeded. No malicious relayer/prover/node assumption is required — the receipt is a genuine, correctly verified proof; the bug is purely in the pallet's failure to branch on the `success` flag it decodes.

### Impact Explanation
This breaks the intended reward invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A failed cross-chain delivery still results in reward payout and permanent removal of the pending order state, i.e., unbacked/undeserved reward minting to relayers and loss of the ability to reprocess or account for the failed message. Given this is BridgeHub/Snowbridge outbound reward accounting (asset/fee accounting), the impact is fund payout to the wrong condition (Medium-High) — the report's "duplicate settlement or payout" / "unbacked mint" analog.

### Likelihood Explanation
Likelihood is Medium: it requires no privileged actor, malicious peer, or governance action — only a legitimately failed/reverted execution on Ethereum (a normal, not-uncommon occurrence, e.g. an out-of-gas dispatch or a reverting command) followed by any relayer submitting the genuine receipt through the public `submit_delivery_receipt` extrinsic.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `success == true`, proceed as today: pay the reward and remove the order.
- If `success == false`, do not pay the reward (or pay a reduced/zero reward), and either retain the order for retry/accounting or transition it to a distinct "failed" state/event instead of silently deleting it as if it were a normal delivery.

### Proof of Concept
1. A message is enqueued and gets a `PendingOrder { nonce, fee, block_number }` via `do_process_message`, as seen in `Messages`/`PendingOrders` insertion logic: [4](#0-3) 
2. On Ethereum, the corresponding command execution reverts, so the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid EventProof for this log (this is a legitimate, correctly-proven event — no forgery needed) and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (real proof), `DeliveryReceipt::try_from` decodes `success: false` correctly, but `process_delivery_receipt` never checks this flag: it looks up the order by nonce, sees `order.fee > 0`, calls `T::RewardPayment::register_reward` to pay the relayer, and removes the order — exactly as it would for a `success: true` receipt. Existing test `submit_delivery_receipt_succeeds_after_unhalt` confirms the pay+remove flow for a receipt without asserting on `success`: [5](#0-4) 
5. Result: reward paid and order cleared despite the underlying cross-chain message execution having failed.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L420-448)
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
```
