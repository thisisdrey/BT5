## Analysis

The external Rocketpool report's core issue is *undocumented fields/values silently influencing (or failing to influence) fund accounting across multiple components*, which increases the risk that a critical semantic distinction gets lost and funds move incorrectly. The closest local, provable analog is in the Snowbridge V2 outbound-queue relayer-reward settlement path, where a decoded but semantically critical boolean (`success`) is never consulted before paying out and permanently clearing the pending order.

### Title
Relayer reward is paid and pending order cleared regardless of on-chain dispatch outcome (`success` field ignored) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decodes the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event and explicitly carries a `success: bool` field [1](#0-0) , but `process_delivery_receipt` never reads or branches on `receipt.success` before paying the relayer reward and deleting the `PendingOrder` from storage [2](#0-1) .

### Finding Description
`submit_delivery_receipt` is a public, unprivileged extrinsic — any signed account can call it as long as it supplies a valid Ethereum event/receipt proof [3](#0-2) . It verifies the proof through `T::Verifier::verify`, decodes the `DeliveryReceipt`, and calls `process_delivery_receipt`. That function checks only:
- the gateway address matches,
- a `PendingOrder` exists for `receipt.nonce`,

then unconditionally calls `T::RewardPayment::register_reward` for `order.fee` and removes the `PendingOrder`, emitting `MessageDelivered` [4](#0-3) .

The `success` field on the `InboundMessageDispatched` Ethereum event is specifically designed to indicate whether the message's commands actually executed successfully on the Gateway contract — it is the on-chain, cryptographically-proven ground truth about dispatch outcome. The pallet's own module doc describes the intended flow as "When the message has been verified and executed, the relayer will call ... `submit_delivery_receipt`" [5](#0-4) , implying reward should track successful execution, yet nothing in the code enforces that link. This is exactly the seed report's pattern: a value (`ethMatched`-like semantic flag) that plays a role in accounting across components (event decoding → receipt struct → pallet settlement) is never actually wired into the accounting decision, and there is no inline documentation explaining why `success` is decoded but discarded.

### Impact Explanation
Because reward payout and pending-order clearance happen unconditionally on proof validity (not on `receipt.success`), the relayer reward is paid and the order state is finalized even for messages whose commands reverted on Ethereum. This violates the required pivot that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here settlement advances even when execution (per the receipt's own `success` flag) did not succeed. This is an economic/accounting correctness bug: funds reserved as delivery fees are disbursed without regard to whether the funded outcome occurred, and the `PendingOrder` is destroyed so there is no route to reconcile, retry, or refund. There is no test in the codebase asserting behavior when `success == false` is submitted (existing tests only cover `success: true` and the "halted" case) [6](#0-5) .

### Likelihood Explanation
Any relayer that observes a genuine but failed `InboundMessageDispatched(success=false)` event on Ethereum can generate a legitimate receipt/execution proof for it (this is real on-chain data, not forged) and submit it via `submit_delivery_receipt`. No privileged role, governance action, or malicious peer/validator is required — this is a standard unprivileged relayer path.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only pay the reward when `success == true`; for `success == false`, define and document an explicit accounting path (e.g., no reward, and either drop/refund the fee to the original sender or leave the order in a distinguishable failed state), and add inline documentation clarifying the intended fund flow for both outcomes, consistent with the recommendation in the seed report to centralize and document accounting explicitly.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { fee, block_number }` [7](#0-6) .
2. On Ethereum, the Gateway processes the message but the embedded commands revert; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid receipt/execution proof for this real event and calls `submit_delivery_receipt(origin, event)` [8](#0-7) .
4. `T::Verifier::verify` succeeds (the proof is genuine), `DeliveryReceipt::try_from` decodes `success: false` correctly [9](#0-8) .
5. `process_delivery_receipt` ignores `receipt.success`, pays `order.fee` to the reward account, removes `PendingOrders[nonce]`, and emits `MessageDelivered` — identical to the success case [4](#0-3) .

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
