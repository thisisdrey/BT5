Audit Report

## Title
`process_delivery_receipt` ignores the `DeliveryReceipt.success` flag, paying relayer rewards and settling orders even on failed Ethereum dispatch - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`DeliveryReceipt` decodes the `InboundMessageDispatched` Ethereum event which carries an explicit `success: bool` "Delivery status" field [1](#0-0) , but `Pallet::process_delivery_receipt` never reads or branches on `receipt.success` before paying the relayer reward and removing the `PendingOrder` [2](#0-1) . This means a relayer submitting a genuine receipt for a failed dispatch on Ethereum still collects the fee and permanently closes the order.

## Finding Description
`submit_delivery_receipt` is callable by any signed account via `ensure_signed(origin)?`, verifies the proof through `T::Verifier::verify`, decodes the log into a `DeliveryReceipt`, and forwards it to `process_delivery_receipt` [3](#0-2) . Inside `process_delivery_receipt`, the only validations performed are a gateway address match (`ensure!(T::GatewayAddress::get() == receipt.gateway, ...)`) and existence of a `PendingOrder` for `receipt.nonce` (`<PendingOrders<T>>::get(nonce).ok_or(...)`) [4](#0-3) . Regardless of `receipt.success`, if `order.fee > 0` the reward is paid via `T::RewardPayment::register_reward`, the order is removed with `<PendingOrders<T>>::remove(nonce)`, and `Event::MessageDelivered` is emitted [5](#0-4) . The `success` field is decoded faithfully from the real on-chain Ethereum event data (`event.success`) via `DeliveryReceipt::try_from` [6](#0-5) , so it is a reality-controlled value that the pallet's payout/settlement logic silently discards.

## Impact Explanation
The `success` field's name and documented purpose ("Delivery status") indicate it is meant to distinguish a dispatch outcome that should gate reward payment and terminal settlement. Because `process_delivery_receipt` pays the reward and irreversibly removes the `PendingOrder` regardless of `success`, a failed Ethereum dispatch is treated identically to a successful one: the relayer is paid and the order can never be retried or resettled for that nonce. This is a fund-flow/payout correctness defect in a public, unprivileged extrinsic that governs bridge reward disbursement and finalizes bridge order state — matching the "duplicate settlement or payout" / "permanent bridge-state lock" impact class, since the pending order for a failed message is finalized/cleared without regard to actual outcome.

## Likelihood Explanation
Any relayer holding a valid Ethereum log proof for a genuine `InboundMessageDispatched` event with `success = false` can submit it through `submit_delivery_receipt` with no privileged origin or governance required — this is the normal relayer workflow, only requiring a real dispatch failure to occur on the Gateway contract. Existing tests in the repo (e.g., `submit_delivery_receipt_succeeds_after_unhalt`) only construct/exercise receipts implicitly assumed successful and never assert behavior for `success: false`, confirming this path is untested and unguarded [7](#0-6) .

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only invoke `T::RewardPayment::register_reward` when `success == true`; for `success == false`, avoid paying the reward and either retain the order for a bounded retry window or transition it to a distinct terminal "failed" state/event rather than unconditionally calling `<PendingOrders<T>>::remove(nonce)` and emitting `Event::MessageDelivered`.

## Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0` [8](#0-7) .
2. The message is relayed to Ethereum and dispatch on the Gateway fails, so the emitted `InboundMessageDispatched` event has `success = false`.
3. Any signed relayer submits this real event log + proof via `submit_delivery_receipt`; `T::Verifier::verify` succeeds because the proof is genuine, and `DeliveryReceipt::try_from` decodes `success: false` faithfully.
4. `process_delivery_receipt` runs its gateway check and nonce lookup, finds `order.fee > 0`, calls `T::RewardPayment::register_reward` and removes the order — identical to the success path — despite `receipt.success == false` never being inspected [5](#0-4) .

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-440)
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

			<Nonce<T>>::set(nonce);

			Self::deposit_event(Event::MessageAccepted { id, nonce });
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L420-448)
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
```
