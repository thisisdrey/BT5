## Title
Relayer reward is paid on `submit_delivery_receipt` without validating the decoded `DeliveryReceipt.success` field - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The external report's core defect is that a critical field decoded from user/relayer-supplied input (`orderId`) was never checked against the business condition it is supposed to represent (whether that specific purchase had already succeeded) before the contract emitted a settlement event and allowed repeated fund movement. The same defect class exists in `snowbridge-pallet-outbound-queue-v2`: the `DeliveryReceipt` decoded from a verified Ethereum event carries a `success: bool` field describing whether the outbound message actually executed correctly on the Ethereum Gateway, but `Pallet::process_delivery_receipt` never reads or enforces this field before paying the relayer reward and clearing the pending order.

## Finding Description
`submit_delivery_receipt` verifies the Merkle/receipt proof of an Ethereum event log and decodes it into a `DeliveryReceipt`: [1](#0-0) 

It then calls `process_delivery_receipt`, which only checks the `gateway` address and looks up the `PendingOrder` by `nonce`: [2](#0-1) 

The `receipt.success` field — present on `DeliveryReceipt` and populated in every test fixture (`success: true`/`success: false` semantics exist by design) — is decoded but never inspected anywhere in the pallet. A `grep` across the entire `bridges/snowbridge` tree shows the only occurrence of `.success` is the field's own definition in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs`; it is never read again in `outbound-queue-v2`, `inbound-queue-v2`, `system-v2`, or `bridge-relayers`.

This mirrors the reported bug precisely: the field that should gate settlement (`orderId`'s existing-purchase state in the report, `success` here) is captured from the incoming payload but the validation function (`validatePurchase` in the report, `process_delivery_receipt` here) never checks it before performing the payout side effect (`register_reward`) and marking the operation as settled (`PendingOrders::remove`).

## Impact Explanation
Because `nonce`-based lookup only proves *that a message with this nonce was queued*, not *that its execution on Ethereum succeeded*, a relayer can submit a genuine, correctly-proven Ethereum event log for a Gateway transaction that reverted or otherwise failed (`success: false`) and still receive the full `order.fee` relayer reward via `T::RewardPayment::register_reward`, exactly as if the message had been delivered successfully. This is an underpriced/undeserved-work payout: rewards are drawn from the sovereign account funding relayer incentives without the corresponding useful delivery work being confirmed, directly matching the "public underpriced work" and "duplicate/incorrect settlement or payout" impact categories in scope.

## Likelihood Explanation
The path is reachable by any relayer submitting a normal, honestly-proven `submit_delivery_receipt` extrinsic — no malicious node, validator, or governance actor is required. The relayer only needs a legitimately failed-but-verifiable Ethereum transaction receipt (e.g. by intentionally under-provisioning gas on the Ethereum-side execution, or by any transaction that reverts post-emission) to trigger reward payment for work that was not actually completed, making this a straightforward, unprivileged-attacker path.

## Recommendation
In `process_delivery_receipt`, check `receipt.success` before paying the reward:
- If `success == false`, do not call `T::RewardPayment::register_reward`; instead emit a distinct failure event (and decide whether/how to retry or refund the pending order) rather than silently deleting `PendingOrders` and rewarding as if delivery succeeded.
- Add a dedicated error/event path and regression tests (mirroring `submit_delivery_receipt_succeeds_after_unhalt`) asserting that a `DeliveryReceipt { success: false, .. }` does NOT result in `RewardRegistered`.

## Proof of Concept
1. A message is queued and committed via `do_process_message`, producing a `PendingOrder { nonce, fee, .. }` in `PendingOrders`.
2. On Ethereum, the corresponding Gateway call reverts (fails) after emitting/including a receipt event, but the transaction receipt/log is still real and Merkle-provable.
3. A relayer builds the `EventProof` for this failed-but-real event and calls:
```rust
OutboundQueue::submit_delivery_receipt(origin, Box::new(event_proof_for_failed_tx));
```
4. `T::Verifier::verify` succeeds (the proof is legitimate), `DeliveryReceipt::try_from` decodes `success: false`.
5. `process_delivery_receipt` proceeds unconditionally: `order.fee > 0` triggers `T::RewardPayment::register_reward(...)`, and `PendingOrders::remove(nonce)` finalizes settlement — identical to the success path, as shown by the existing test `submit_delivery_receipt_succeeds_after_unhalt` which never varies `success` and still checks only `RewardRegistered`/`MessageDelivered`. [3](#0-2)

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L445-481)
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
