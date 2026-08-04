### Title
Relayer reward paid and pending order settled regardless of on-chain execution outcome (`DeliveryReceipt.success` never checked) - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet pays the relayer reward and clears the `PendingOrders` entry using only the `gateway` address and `nonce` fields of a verified `DeliveryReceipt`. The `success` field, which the Ethereum `InboundMessageDispatched` event carries to indicate whether the corresponding message actually executed successfully on Ethereum, is decoded but never read or enforced anywhere in the pallet.

### Finding Description
The `DeliveryReceipt` type decoded from the proven Ethereum event log contains a `success: bool` field: [1](#0-0) 

`submit_delivery_receipt` verifies the event proof, decodes the receipt, and forwards it to `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` only checks the `gateway` address and looks up the `PendingOrder` by `nonce`; it never inspects `receipt.success` before paying the fee and removing the order: [3](#0-2) 

A repo-wide search confirms `success` is defined only in the `DeliveryReceipt` struct/decoder and is never referenced by any consumer of the receipt (`process_delivery_receipt`, `lib.rs`, `send_message_impl.rs`, `process_message_impl.rs`), i.e. the field is decoded from the Ethereum-side proof but silently discarded.

This mirrors the reported bug class: a value that is supposed to gate whether a benefit (bond purchase price / here, relayer reward and order settlement) is granted is decoded but not validated against the state that determines correctness (issuance index in the report; execution outcome here). The existing guards — gateway-address equality and nonce-based `PendingOrders` lookup — only prove that *a* message with that nonce was queued and that *some* event was emitted by the Gateway; they say nothing about whether the message dispatch on Ethereum actually succeeded.

### Impact Explanation
Per the design comment in the same file, `submit_delivery_receipt` is meant to "verify the message with proof ... fetch the pending order by nonce ... pay reward with fee attached" only once delivery is confirmed: [4](#0-3) 

Because `success` is never enforced, any signed account can submit a valid-proof receipt for a nonce whose Ethereum-side execution failed (`success == false`) and still: (1) have `T::RewardPayment::register_reward` pay out the full `order.fee`, and (2) have the `PendingOrder` removed from `PendingOrders`, permanently closing out that order as if it were settled. This breaks the "payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant for the bridge reward flow — underpriced/incorrect work is rewarded, and a failed delivery can never be retried or reconciled because its `PendingOrder` is deleted regardless of outcome.

### Likelihood Explanation
High for an unprivileged actor with a valid Ethereum receipt: any relayer (or anyone who can construct/obtain the required storage/execution proof for a `InboundMessageDispatched` event with `success = false`) can call `submit_delivery_receipt` through the standard signed-origin extrinsic. No governance, admin, validator, or off-chain relayer collusion is required — this is a public dispatch entrypoint reachable by any signed account.

### Recommendation
In `process_delivery_receipt`, validate `receipt.success` before paying the reward and/or before removing the `PendingOrder`. Concretely:
- If `receipt.success == false`, do not call `T::RewardPayment::register_reward`, or reward a reduced/zero amount per policy.
- Decide whether a failed delivery should still remove the `PendingOrder` (closing the order) or be retried; if retried, do not clear `PendingOrders` on failure so a corrected/re-executed delivery can later be settled correctly.
- Emit a distinct event (e.g. `MessageDeliveryFailed`) for `success == false` so relayers/dApps can observe and react instead of silently treating failed executions the same as successful ones.

### Proof of Concept
1. A message is enqueued in `do_process_message`, creating `PendingOrders::<T>::insert(nonce, PendingOrder { nonce, fee, block_number })` with `fee > 0`: [5](#0-4) 
2. On Ethereum, the message dispatch fails (e.g., the target contract call reverts), and the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer (or any signed account with access to the transaction receipt/proof) calls `submit_delivery_receipt(origin, event)` with this proof. `T::Verifier::verify` succeeds (the event genuinely occurred), and `DeliveryReceipt::try_from` decodes `success = false`.
4. `process_delivery_receipt` executes: `receipt.gateway` matches `T::GatewayAddress`, `PendingOrders::get(nonce)` returns `Some(order)`, and because `order.fee > 0` the pallet calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` — paying the reward exactly as if the message had succeeded — then removes the `PendingOrder` and emits `Event::MessageDelivered { nonce }`, even though delivery failed.

This can be reproduced by adapting the existing test `submit_delivery_receipt_succeeds_after_unhalt` (which uses `mock_valid_event_proof()`/`success: true`) to instead construct/mock a receipt with `success: false` and asserting that the reward is still registered and the order is still removed: [6](#0-5)

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
