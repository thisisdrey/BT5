The code confirms the claim exactly as described. `process_delivery_receipt` at line 466 gates the reward payout solely on `order.fee > 0`, never reading `receipt.success` anywhere in the function body.Audit Report

## Title
Relayer reward paid on failed Ethereum message delivery because `DeliveryReceipt.success` is never checked - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` that carries a `success` field indicating whether the dispatch on Ethereum actually succeeded, but the reward-payout logic never reads that field. Payment is gated solely on `order.fee > 0`, so a relayer submitting a valid proof of a *failed* delivery (`success: false`) is paid exactly the same as for a successful one.

## Finding Description
The Gateway contract event `InboundMessageDispatched(nonce, topic, success, reward_address)` is decoded faithfully into `DeliveryReceipt::success` in `TryFrom<&Log> for DeliveryReceipt`: <cite repo="Kohvert/polkadot-sdk--029" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-27" end="35-51" /> [1](#0-0) 

The unprivileged, signed extrinsic `submit_delivery_receipt` verifies the proof and calls `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` only checks the gateway address and `order.fee > 0` before paying the reward, never inspecting `receipt.success`, and then unconditionally removes the order and emits `MessageDelivered`: [3](#0-2) 

A `grep_search` for `success` scoped to `bridges/snowbridge/pallets/outbound-queue-v2/` returned zero matches, confirming the field is decoded upstream but never referenced anywhere in this pallet's logic. The existing guards — gateway-address check, `T::Verifier::verify` (proves only that the event log was genuinely emitted, regardless of its `success` value), and the `PendingOrders` lookup (proves only that the order is still pending) — do not address the outcome of the delivery at all.

## Impact Explanation
This allows unbacked/duplicate-style payout: a relayer who submits a legitimate, verifiable proof of a failed Ethereum dispatch (`success: false`) still receives the full `order.fee` reward via `T::RewardPayment::register_reward`, exactly as if the message had succeeded. This directly violates the invariant that "payout state must only advance after execution... succeed," draining the reward pool for deliveries that did not achieve their intended effect on Ethereum.

## Likelihood Explanation
The path requires only calling the unprivileged, signed extrinsic `submit_delivery_receipt` with a real, verifiable Ethereum event proof — no malicious relayer collusion or off-chain infrastructure compromise is needed, since failed dispatches (e.g., due to gas exhaustion or reverts inside a `Command`) are a normal occurrence in cross-chain message execution, not an artificially constructed edge case.

## Recommendation
Gate the reward payment in `process_delivery_receipt` on `receipt.success` in addition to `order.fee > 0`, e.g. `if order.fee > 0 && receipt.success { ... register_reward ... }`, and consider emitting a distinct event (e.g. `MessageDeliveryFailed`) for the `success == false` case so failed deliveries are observable without being rewarded.

## Proof of Concept
1. A message is queued via `do_process_message`, inserting a `PendingOrder { fee: F, .. }` with `F > 0` into `PendingOrders`. [4](#0-3) 
2. On Ethereum, the Gateway processes the message but the dispatch fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address=relayer)`.
3. Any relayer obtains a valid proof for this real event and calls `submit_delivery_receipt`. `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly.
4. `process_delivery_receipt` still executes `if order.fee > 0 { register_reward(...) }`, pays the fee in full, removes the order, and emits `MessageDelivered` — identical behavior to a successful delivery. This is confirmed by the existing test `submit_delivery_receipt_succeeds_after_unhalt`, which never sets `success: false` and whose flow relies solely on `order.fee` for the payout condition. [5](#0-4)

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L44-50)
```rust
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
