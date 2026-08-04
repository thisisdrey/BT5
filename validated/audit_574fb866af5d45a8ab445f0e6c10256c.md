## Finding

The Snowbridge `outbound-queue-v2` pallet's `process_delivery_receipt` function pays out the relayer reward based solely on the existence of a `PendingOrder` and a nonzero `fee`, while completely ignoring the `success` field of the decoded `DeliveryReceipt`. This is a structurally identical bug class to the external report: a condition/value that is supposed to gate an action (reward payout / compartmentalization) is decoded but not enforced, so the action proceeds even when it shouldn't.

### Title
Relayer reward is paid on `submit_delivery_receipt` regardless of the receipt's `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decodes a `success: bool` field from the Ethereum `InboundMessageDispatched` event log [1](#0-0) , but `Pallet::process_delivery_receipt` never reads or checks this field before releasing the reward — it only checks `order.fee > 0` [2](#0-1) .

### Finding Description
The extrinsic `submit_delivery_receipt` verifies the Ethereum log proof, decodes it into a `DeliveryReceipt`, and calls `process_delivery_receipt`: [3](#0-2) 

Inside `process_delivery_receipt`, the checks performed are:
1. `receipt.gateway` matches the configured gateway.
2. A `PendingOrder` exists for `receipt.nonce`.

Then, if `order.fee > 0`, the reward is unconditionally paid via `T::RewardPayment::register_reward`, and the order is removed: [4](#0-3) 

The `success` field of the receipt — populated straight from the on-chain `InboundMessageDispatched(nonce, topic, bool success, bytes32 reward_address)` event — is never inspected: [1](#0-0) 

This mirrors the reported pattern exactly: a guard value is computed/decoded but the branch that should depend on it (pay only on success) is missing, so the payout path executes unconditionally, just like `compartmentalize()` executing even when `isBorrowLimitHit` reasoning was supposed to prevent it.

### Impact Explanation
Any signed account that can produce a valid Ethereum log + proof for a message nonce that has a `PendingOrder` (i.e., any relayer who gets the message dispatched on Ethereum, successfully or not) can claim the full fee attached to that order regardless of whether the message actually executed successfully on the destination (Ethereum) side. This is a fund-loss/incorrect-payout condition: BridgeHub reward funds are settled to a relayer for work whose outcome (`success = false`) should arguably have been distinguished (e.g., no reward, or partial reward, or requeue), but instead a full, unconditional payout occurs and the `PendingOrder` is deleted, closing off any future settlement for that nonce. This falls under "duplicate settlement or payout" / value not conserved to the "rightful" outcome from the impact gate.

### Likelihood Explanation
This is reachable via the fully public, unprivileged extrinsic `submit_delivery_receipt` [3](#0-2)  — no governance, admin, or malicious-peer assumption is required. The only requirement is a genuine, verifiable Ethereum event log for the gateway with `success = false`, which is a normal/expected outcome for messages whose Ethereum-side commands revert (e.g., out-of-gas dispatch, reverting command execution) — not an attacker-crafted or adversarial input.

### Recommendation
Gate the reward payout (and/or the order removal / requeue logic) on `receipt.success`. For example, only call `T::RewardPayment::register_reward` when `receipt.success` is `true`; when `false`, either withhold the reward, apply a different reward policy, or leave/mark the order for reprocessing rather than unconditionally removing it and paying in full.

### Proof of Concept
1. A message is enqueued and a `PendingOrder { nonce, fee, .. }` is created with `fee > 0` via `do_process_message` [5](#0-4) .
2. The message is delivered to Ethereum but its dispatch fails on-chain (Ethereum gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`).
3. A relayer (or the failing party's collaborator) submits this log with a valid proof to `submit_delivery_receipt`.
4. `process_delivery_receipt` decodes `success = false` but never checks it; since `order.fee > 0`, the reward is paid in full via `T::RewardPayment::register_reward`, and the `PendingOrder` is removed [6](#0-5) .
5. Existing tests such as `submit_delivery_receipt_succeeds_after_unhalt` and `invalid_nonce_for_delivery_receipt_fails` [7](#0-6)  confirm the pallet's happy-path checks (gateway, nonce, halted-state) but there is no test or code path asserting reward is withheld when `success = false`.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-436)
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
