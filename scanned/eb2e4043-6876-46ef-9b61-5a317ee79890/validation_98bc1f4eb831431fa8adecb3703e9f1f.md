## Analysis

The external report's core broken invariant: **a validity/staleness signal exists in the data model but the acceptance check uses a threshold/field that doesn't actually enforce it**, so invalid data is treated as valid and downstream logic (price usage) proceeds on bad input.

The closest local analog is in the Snowbridge V2 outbound queue reward-settlement path. The `DeliveryReceipt` type decoded from the Ethereum `InboundMessageDispatched` event log carries a `success: bool` field indicating whether the relayed message actually executed successfully on the Gateway contract, but `Pallet::process_delivery_receipt` never inspects this field before paying the relayer reward and clearing the pending order.

### Title
Relayer reward is paid and pending order is settled regardless of on-chain execution outcome (`receipt.success` is decoded but never checked) - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`DeliveryReceipt` decoded from the `InboundMessageDispatched` Ethereum event includes a `success` flag reporting whether the message actually executed successfully on the Gateway contract [1](#0-0) . `Pallet::process_delivery_receipt` only validates the gateway address and the pending nonce, then unconditionally pays the fee and removes the order, never reading `receipt.success` [2](#0-1) .

### Finding Description
`submit_delivery_receipt` verifies the Merkle/event proof via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt`, and forwards it to `process_delivery_receipt` [3](#0-2) . In `process_delivery_receipt`, only `receipt.gateway` and the existence of a `PendingOrders` entry for `receipt.nonce` are checked before the reward is registered and the order removed:

```rust
ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
...
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
``` [4](#0-3) 

`receipt.success` is populated from the on-chain event (`success: event.success`) but is dead data in the settlement path [5](#0-4) . This is the direct analog of the Chainlink report's flaw: a field/signal that exists specifically to gate acceptance of "good" state is present but not wired into the actual guard, so the guard silently accepts an outcome (a *failed* Ethereum-side dispatch) as if it were the valid outcome (a *successful* dispatch).

### Impact Explanation
This falls under the "duplicate settlement or payout" / "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot. Any relayer can submit a proof for an `InboundMessageDispatched` event where `success == false` (i.e., the command reverted or failed on the Gateway contract on Ethereum) and still: (1) collect the full relayer fee via `T::RewardPayment::register_reward`, and (2) have the `PendingOrder` removed and a `MessageDelivered` event emitted, permanently closing out the order as if delivery succeeded. This both mints/pays unbacked rewards for non-executed work and permanently discards the record of a failed delivery, with no path to retry, penalize, or re-settle it — a bridge-state accounting break, not just a cosmetic log issue.

### Likelihood Explanation
No privileged actor is required — any account holding a valid Ethereum event proof for a call that reverted on the Gateway (`success = false`) can submit it via the public, signed `submit_delivery_receipt` extrinsic [3](#0-2) . Ethereum-side command execution failures (e.g., an XCM-derived command reverting due to insufficient gas budget, a downstream contract call failing, etc.) are a normal occurrence, not an adversarial edge case, making this readily triggerable in practice, not merely theoretical.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`. For `success == false`, still remove/settle the `PendingOrder` (so nonces don't stay open forever) but do not pay a reward, and consider emitting a distinct event (e.g. `MessageDispatchFailed { nonce }`) so failed deliveries are observable and reward accounting stays honest.

### Proof of Concept
1. A message is queued and gets a `PendingOrder { nonce, fee, .. }` via `do_process_message` [6](#0-5) .
2. On Ethereum, the relayer submits the message to the Gateway; the command execution reverts, but the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` (per the tested event decode, `success` is a real field independent of `PendingOrder` state) [7](#0-6) .
3. Relayer obtains the standard event/tx receipt proof and calls `submit_delivery_receipt` with this proof; `T::Verifier::verify` succeeds because it only proves the event happened, not that `success == true`.
4. `process_delivery_receipt` finds `order.fee > 0`, calls `register_reward` for the full fee, and removes the `PendingOrder`, emitting `MessageDelivered` — identical outcome to a genuinely successful delivery, confirmed by existing tests exercising this exact call path without any success-based branching [8](#0-7) .

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L41-50)
```rust
		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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
