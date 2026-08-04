Based on my investigation, I found a genuine local analog: a decoded field from the Ethereum event log that is silently ignored in the accounting logic, causing rewards to be paid regardless of the actual outcome the field was meant to represent.

### Title
`submit_delivery_receipt` pays and settles relayer reward regardless of on-chain dispatch outcome on Ethereum - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The `DeliveryReceipt` type decoded from the Ethereum `InboundMessageDispatched` event includes a `success: bool` field indicating whether the message dispatch on Ethereum actually succeeded [1](#0-0) . However, `Pallet::process_delivery_receipt` never reads or checks this field: it unconditionally pays the reward from the `PendingOrder` and removes the order from `PendingOrders`, regardless of whether `receipt.success` is `true` or `false` [2](#0-1) .

### Finding Description
This mirrors the bug class in the external report: a struct/interface field is decoded from the "correct" schema but the consuming logic acts as if it matches an older/incomplete schema, so a semantically meaningful field is dropped from the control-flow decision. Here, `success` is present in the decoded Solidity event and struct [1](#0-0)  but `process_delivery_receipt` uses only `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce`: [3](#0-2) 

The pallet doc explicitly states the intended flow is: "When the message has been verified and executed, the relayer will call ... to: a. Verify the message with proof ... b. Fetch the pending order by nonce ..., pay reward with fee attached in the order c. Remove the order" [4](#0-3) . This implies reward payment should be conditioned on successful execution, since the whole point of tracking `success` in the Ethereum event is to distinguish a message that dispatched successfully from one that reverted/failed on the Ethereum Gateway. Because the field is ignored, `PendingOrders` is unconditionally settled (removed) and the reward is unconditionally registered as long as `order.fee > 0`, whether or not the message actually executed on Ethereum.

### Impact Explanation
This breaks the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant. Concretely:
- A relayer can submit a valid-proof receipt for a message whose Ethereum-side dispatch failed (`success == false`) and still collect the full relayer reward as if delivery succeeded.
- The `PendingOrder` is removed on any receipt regardless of `success`, permanently closing out the order — there is no retry/compensation path visible in this pallet for a failed dispatch, since `PendingOrders::remove(nonce)` happens unconditionally [5](#0-4) .
- This is an unbacked/incorrect payout: value is settled to the relayer beneficiary independent of the truthfulness of the underlying execution outcome that the receipt is supposed to certify.

This is a "theft or unbacked mint" / "duplicate or incorrect settlement" class impact under the specified gate, since it produces payout regardless of whether the corresponding cross-chain work was actually completed, and it is reachable via a normal signed extrinsic (`submit_delivery_receipt`) with a legitimately verified proof — no malicious relayer/validator assumption is required beyond a relayer submitting a real receipt for a message that happened to fail on Ethereum (which can occur naturally, e.g. due to insufficient gas or reentrancy protection on the Gateway, not attacker malice).

### Likelihood Explanation
Likelihood is moderate-to-high: any message dispatch that fails on the Ethereum Gateway (revert, out-of-gas, guarded call, etc.) will emit `InboundMessageDispatched(nonce, topic, success=false, reward_address)`. Any relayer (not necessarily malicious) who submits that legitimate event log and inclusion proof through `submit_delivery_receipt` triggers the unconditional reward payment and order removal, since `T::Verifier::verify` only checks the cryptographic validity/inclusion of the log, not the semantic `success` value [6](#0-5) .

### Recommendation
- Check `receipt.success` in `process_delivery_receipt` before paying the reward.
- Decide and implement the correct behavior for `success == false`: e.g., do not pay reward, and either keep the `PendingOrder` for a future correction path or emit a distinct `MessageDeliveryFailed` event instead of `MessageDelivered`, so failed dispatches are not silently settled as successful.
- Add a regression test asserting that a receipt with `success: false` does not trigger `RewardRegistered`.

### Proof of Concept
Based on the existing test harness pattern in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` (e.g. `submit_delivery_receipt_succeeds_after_unhalt`):
1. Insert a `PendingOrder { nonce, fee: 1_000_000, .. }` into `PendingOrders`.
2. Construct a `DeliveryReceipt { gateway, nonce, topic, success: false, reward_address }` (mirroring `mock_valid_event_proof` but with `success: false` in the underlying `InboundMessageDispatched` log).
3. Call `OutboundQueue::submit_delivery_receipt(origin, event)`.
4. Observe: the call succeeds, `PendingOrders::get(nonce)` becomes `None`, and `pallet_bridge_relayers::Event::RewardRegistered` is emitted — identical to the `success: true` path shown in `submit_delivery_receipt_succeeds_after_unhalt` [7](#0-6)  — demonstrating the reward and settlement are paid irrespective of the `success` flag.

Note: I could not fully trace how `Verifier::verify` and the beacon/receipt proof pipeline validate the raw log bytes to rule out any upstream filtering of `success`; this analysis is based on the pallet-level logic shown above and the visible test fixtures. A Devin session with full repo/test execution access would be needed to run the concrete PoC and confirm no other layer intercepts `success` before `process_delivery_receipt` is reached.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L308-317)
```rust

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
