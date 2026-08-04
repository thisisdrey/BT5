## Analysis

The external report's core broken invariant is: **a system exposes/acts on state that is supposed to reflect a real-world event's outcome, but nothing enforces that the recorded outcome matches reality before it is trusted/acted upon** (OpenSea trusts a cached "stream status" that no longer reflects the true, updated state).

The closest local analog is in Snowbridge's `snowbridge-pallet-outbound-queue-v2`. The `DeliveryReceipt` decoded from an Ethereum event log carries a `success: bool` field that is supposed to reflect whether the message actually executed successfully on Ethereum, but `process_delivery_receipt` never inspects it — it treats every syntactically valid receipt as a successful delivery and pays the reward / clears the order regardless of the real outcome.

### Title
Relayer reward is paid and `PendingOrder` is settled without checking `DeliveryReceipt::success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event log includes a `success` boolean field indicating whether the corresponding message actually executed successfully on the Ethereum Gateway contract. `Pallet::process_delivery_receipt` decodes and stores this field on the struct but never reads it when deciding to pay the relayer reward and remove the `PendingOrder`.

### Finding Description
The receipt type explicitly carries delivery status: [1](#0-0) 

However, `process_delivery_receipt` only validates the gateway address and the pending nonce; it pays the reward and removes the order unconditionally, without ever branching on `receipt.success`: [2](#0-1) 

The doc comment for the pallet even states the intended flow — "When the message has been verified and executed, the relayer will call ... `submit_delivery_receipt`" — implying settlement should only occur on genuine successful execution: [3](#0-2) 

Because `T::Verifier::verify` only checks that the log/proof are cryptographically included in a finalized Ethereum block (i.e., that the event genuinely happened), and does not itself encode "success" semantics, a legitimately-emitted `InboundMessageDispatched(nonce, topic, success=false, reward_address)` event — which Ethereum emits even when the dispatched command reverts/fails on the Gateway — is accepted exactly the same way as a `success=true` event: [4](#0-3) 

This violates the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — settlement (reward payment + `PendingOrders` removal) advances on receipt of *any* valid event, not on verified successful execution.

### Impact Explanation
A relayer can be rewarded for "delivering" a message that actually failed to execute on Ethereum (e.g. reverted due to insufficient gas or a contract error), while the `PendingOrder` is deleted as if it were properly settled. This is a duplicate/incorrect settlement: the relayer's fee is paid out for work that produced no successful outcome, and there is no retry/re-queue path once the order is removed, permanently losing track of the failed message on the Substrate side while still paying for it. This directly maps to the "duplicate settlement or payout" and "public underpriced work" impact categories called out in the required-impacts list.

### Likelihood Explanation
This is reachable by any unprivileged relayer account via the public `submit_delivery_receipt` extrinsic. It requires no malicious validator, node, prover, or governance action — only a normal relayer submitting a genuinely emitted (and genuinely provable) `success=false` event log for a message whose execution failed on Ethereum, which is an expected occurrence in normal operation (e.g., out-of-gas commands), not an attacker-crafted proof.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only pay the reward when `success == true`; on `success == false`, either re-queue/retry the message, or remove the order without paying the reward (and emit a distinct `MessageDeliveryFailed` event), so the on-chain settlement state accurately reflects the actual Ethereum-side outcome.

### Proof of Concept
1. A message is enqueued and processed via `do_process_message`, creating `PendingOrders[nonce] = { fee > 0, block_number }`. [5](#0-4) 
2. On Ethereum, the corresponding command execution fails/reverts inside the Gateway; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` (this is standard "receipt" behavior for failed sub-calls in EVM logs).
3. A relayer submits `submit_delivery_receipt` with a valid inclusion proof of that `success=false` log. `T::Verifier::verify` succeeds (it only proves inclusion), `DeliveryReceipt::try_from` decodes `success: false` without error. [6](#0-5) 
4. `process_delivery_receipt` ignores `receipt.success`, pays `order.fee` to `reward_account`, and removes the order — identical to the success path, confirmed by the existing test `submit_delivery_receipt_succeeds_after_unhalt`, which only checks halted-vs-unhalted state and never varies `success`. [7](#0-6)

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
