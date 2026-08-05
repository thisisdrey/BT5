Audit Report

## Title
`submit_delivery_receipt` pays relayer reward without checking `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
The `DeliveryReceipt` struct decoded from the Ethereum `InboundMessageDispatched` event carries an explicit `success: bool` field indicating whether the relayed message actually executed successfully on the Gateway contract, but `process_delivery_receipt` never reads or branches on this field before paying out the relayer's reward. As a result, any relayer submitting a valid proof of a `InboundMessageDispatched` log — regardless of whether `success` is `true` or `false` — receives the full `order.fee` payout and the `PendingOrder` is unconditionally removed.

## Finding Description
`DeliveryReceipt` decodes the Solidity event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` and stores the decoded `success` value in the `success` field of the struct [1](#0-0) , and `TryFrom<&Log>` populates it directly from the event without any validation of its value [2](#0-1) .

The public, unprivileged extrinsic `submit_delivery_receipt` verifies the event proof via `T::Verifier::verify`, decodes the receipt, and forwards it directly to `process_delivery_receipt` with no further gating [3](#0-2) .

`process_delivery_receipt` only checks that (a) `receipt.gateway` matches `T::GatewayAddress`, and (b) a `PendingOrder` exists for `receipt.nonce`. It then unconditionally calls `T::RewardPayment::register_reward` with `order.fee` and removes the `PendingOrder`, without ever reading `receipt.success` [4](#0-3) . The module documentation explicitly states the intended contract is that reward is paid "when the message has been verified and executed" [5](#0-4) , but the implementation drops the execution-success condition entirely.

## Impact Explanation
This violates the invariant that payout state must only advance after execution and settlement actually succeed. A relayer can be paid the full `order.fee` for a message whose Ethereum-side execution reverted or otherwise failed (`success == false`), while the corresponding `PendingOrder` is still permanently removed from storage as if delivery succeeded. This is an unbacked/incorrect payout of bridge reward funds that does not correspond to a successful delivery, falling under the "theft or unbacked...payout" / "duplicate settlement or payout" impact category.

## Likelihood Explanation
High. No privileged, admin, or malicious-peer assumption is required — an ordinary relayer only needs a valid proof for any legitimately emitted `InboundMessageDispatched` log for a nonce that still has a pending order (success or not). Execution failures on the Ethereum Gateway (e.g., reverts from gas mis-estimation or bad payload handling) are a normal occurrence, making this a trivially and repeatedly triggerable issue rather than an attacker-crafted edge case.

## Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying the reward. On `success == false`, withhold or reduce the reward (or route to an alternative reward/penalty path) instead of paying the full `order.fee`, and only then remove the `PendingOrder`.

## Proof of Concept
1. `do_process_message` queues a message, creating `PendingOrder { nonce: N, fee: F, block_number }` [6](#0-5) .
2. The relayer relays it to Ethereum; the Gateway contract execution fails, emitting `InboundMessageDispatched` with `success = false` but matching `nonce`/`topic`.
3. The relayer obtains a valid proof of this event and calls `submit_delivery_receipt(origin, event)`; `T::Verifier::verify` succeeds since the log is real, and `DeliveryReceipt::try_from` decodes `success: false` without error.
4. `process_delivery_receipt` proceeds through the gateway-address check and nonce lookup, pays `order.fee` to `reward_account`, and removes the `PendingOrder` — identical to the success path [7](#0-6) .
5. No existing test (e.g. `submit_delivery_receipt_succeeds_after_unhalt`) exercises `success == false` to confirm rejection or reward reduction, confirming the gap is unguarded in the current test suite [8](#0-7) .

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-51)
```rust
impl TryFrom<&Log> for DeliveryReceipt {
	type Error = DeliveryReceiptDecodeError;

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
