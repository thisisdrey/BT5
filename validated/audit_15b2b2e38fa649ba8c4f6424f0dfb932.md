### Title
Relayer reward paid on `submit_delivery_receipt` regardless of message execution outcome — (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is: a state-transition (decryption/ciphertext acceptance) is authenticated only on format, not on the actual semantic success of the operation, letting an attacker get a positive result despite the underlying operation having failed. The local analog is in the Snowbridge outbound queue v2 payout path: `process_delivery_receipt` verifies only that the event log came from the right gateway and that a `PendingOrder` exists for the nonce, but it never checks the `success` field of the decoded `DeliveryReceipt`, which is exactly the field Ethereum emits to signal whether the message actually executed successfully on the destination.

### Finding Description
`DeliveryReceipt` is decoded from the `InboundMessageDispatched` Ethereum event, which explicitly carries a `success: bool` field alongside `nonce`, `topic`, and `reward_address`: [1](#0-0) 

`submit_delivery_receipt` verifies the cryptographic proof of the event log and then calls `process_delivery_receipt`: [2](#0-1) 

Inside `process_delivery_receipt`, the pallet checks the gateway address, resolves the `reward_account`, fetches the `PendingOrder` by `receipt.nonce`, and unconditionally pays `order.fee` to the relayer/reward account before removing the pending order — `receipt.success` is never read or branched on: [3](#0-2) 

A repo-wide search confirms `receipt.success`/`.success` is referenced only at decode time in `delivery_receipt.rs` and nowhere in the payout logic of `outbound-queue-v2`, so the field is decoded and then discarded. This differs from the trust model documented in the pallet's own module docs, which describe the receipt as proof that "the message has been verified and executed" before reward payment — but the code never confirms the "executed successfully" part, only that a log with that nonce exists and was included in a valid receipt proof: [4](#0-3) 

This is the direct structural analog to the CryptoJS finding: an authentication/verification step exists (`T::Verifier::verify`, gateway check, nonce lookup) but the actual semantic correctness of the underlying operation (message *successfully* dispatched on Ethereum, i.e. `success == true`) is not bound into the acceptance decision, so the guard "verification succeeded" is conflated with "the thing being paid for actually happened."

### Impact Explanation
Any relayer can submit a genuine `InboundMessageDispatched` event where Ethereum-side execution reverted or failed (`success = false`) but the on-chain call still verifies the proof and pays out `order.fee` from `T::RewardPayment`, and permanently removes the `PendingOrder`. This is an underpriced/incorrect public-payout bug: reward funds are settled to a relayer for work that was not actually completed, i.e. "public underpriced work" / "duplicate or wrong-condition payout" from the impact gate, without requiring a malicious validator, governance actor, or leaked key — an ordinary relayer submitting a normal, unmodified but failed-execution receipt triggers it.

### Likelihood Explanation
Failed dispatches on the Ethereum side (e.g., reverted commands, out-of-gas, target contract errors) are a realistic and expected occurrence in a cross-chain bridge, and the `success` flag exists precisely to distinguish this case. Any relayer whose message happens to fail on Ethereum, or who is incentivized to game the reward flow, needs no special privileges to call `submit_delivery_receipt` with an honestly-obtained but "unsuccessful" event proof and still collect the reward.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying `order.fee`; only pay the reward when `success == true`. For unsuccessful deliveries, define separate handling (e.g., remove/expire the pending order without reward, or allow retry/refund logic) so payout state only advances after the message is confirmed to have executed successfully, consistent with the "settle exactly once to the rightful beneficiary and amount" requirement.

### Proof of Concept
1. A relayer relays a v2 outbound message to Ethereum; Ethereum's Gateway attempts dispatch, but the destination command reverts, so the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
2. The relayer submits this genuine event log + valid inclusion/execution proof via `submit_delivery_receipt`.
3. `T::Verifier::verify` succeeds (the proof is valid), `DeliveryReceipt::try_from` decodes `success = false` but the field is never inspected.
4. `process_delivery_receipt` finds the matching `PendingOrder`, pays `order.fee` to the reward account, and removes the order — identical to the success path, as shown in `submit_delivery_receipt_succeeds_after_unhalt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` (lines 420-448), which asserts payout/removal purely based on verifier state, without any test asserting reward is withheld when `success = false`. [5](#0-4)

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
