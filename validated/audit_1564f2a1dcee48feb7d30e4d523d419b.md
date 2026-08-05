This confirms the claim: `receipt.success` is decoded but never referenced anywhere in the outbound-queue-v2 pallet (`.success` field usage confined to the decode struct definition itself, not the consuming pallet logic), and `process_delivery_receipt` only emits `MessageDelivered` unconditionally with no `MessageDeliveryFailed` counterpart.

Audit Report

## Title
`submit_delivery_receipt` pays and settles relayer reward regardless of on-chain dispatch outcome on Ethereum - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
The `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field indicating whether the message actually executed successfully on the Ethereum Gateway. `Pallet::process_delivery_receipt` never reads this field, so it unconditionally pays the relayer reward and removes the `PendingOrder` whenever `order.fee > 0`, whether the underlying dispatch on Ethereum succeeded or reverted.

## Finding Description
`DeliveryReceipt` includes `success` decoded straight from the Solidity event log [1](#0-0) , and the decode implementation faithfully populates it from `event.success` [2](#0-1) . The call handler `submit_delivery_receipt` verifies proof validity and decodes the receipt, then forwards it to `process_delivery_receipt` without ever inspecting `success` [3](#0-2) . Inside `process_delivery_receipt`, only `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` are used; the reward is registered whenever `order.fee > 0`, and `PendingOrders::remove(nonce)` runs unconditionally right after, with a single `MessageDelivered` event emitted regardless of outcome [4](#0-3) . A repo-wide search confirms `receipt.success` is not read anywhere in the pallet logic — the only occurrence of `.success` in the crate is the field definition/assignment in the primitive decode type, not any conditional in the pallet. There is also no `MessageDeliveryFailed`-style event defined for the failure path.

The pallet's own doc comment states the intended flow is to verify the message, then "fetch the pending order by nonce ..., pay reward with fee attached in the order" and "remove the order" — it does not condition this on the dispatch outcome in either the doc or the code, confirming the field is dropped from the control flow rather than intentionally omitted for a different design reason.

## Impact Explanation
This violates the invariant that bridge markers, receipts, and payout state must only advance after execution and settlement succeed atomically. A relayer submitting a legitimately verified receipt for a message that failed to execute on the Ethereum Gateway (revert, out-of-gas, reentrancy guard, etc.) still receives the full reward via `T::RewardPayment::register_reward`, and the corresponding `PendingOrder` is permanently removed with no retry or compensation path in this pallet. This is an incorrect/unbacked payout condition matching the "duplicate settlement or payout" / "theft or unbacked mint or unlock" impact class, since value is settled independent of whether the cross-chain work actually completed.

## Likelihood Explanation
Likelihood is moderate: any Ethereum-side dispatch failure naturally produces `InboundMessageDispatched(nonce, topic, success=false, reward_address)`, and any relayer — malicious or not — who submits that legitimate event log with its inclusion proof through the public `submit_delivery_receipt` extrinsic triggers the unconditional reward and settlement, since `T::Verifier::verify` only checks cryptographic proof validity, not the semantic `success` value.

## Recommendation
Check `receipt.success` in `process_delivery_receipt` before calling `register_reward`. For `success == false`, avoid paying the reward and either retain the `PendingOrder` for a defined recovery/retry path or emit a distinct failure event (e.g., `MessageDeliveryFailed`) instead of `MessageDelivered`, so failed dispatches are not silently settled as successful deliveries. Add a regression test asserting a receipt with `success: false` does not emit `RewardRegistered`.

## Proof of Concept
Using the existing test harness pattern (`submit_delivery_receipt_succeeds_after_unhalt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs`):
1. Insert a `PendingOrder { nonce, fee: 1_000_000, .. }` into `PendingOrders`.
2. Build a mock `EventProof` whose underlying `InboundMessageDispatched` log has `success: false`.
3. Call `OutboundQueue::submit_delivery_receipt(origin, event)`.
4. Observe the call succeeds, `PendingOrders::get(nonce)` becomes `None`, and `pallet_bridge_relayers::Event::RewardRegistered` is emitted — identical to the `success: true` path in the existing test — demonstrating reward and settlement occur irrespective of the `success` flag. [5](#0-4)

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L38-51)
```rust
	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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
