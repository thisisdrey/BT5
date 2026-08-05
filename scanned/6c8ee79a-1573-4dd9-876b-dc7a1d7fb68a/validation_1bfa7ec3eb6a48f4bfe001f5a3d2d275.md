Confirmed: `receipt.success` is decoded from the Ethereum `InboundMessageDispatched` event log but the `success` field is never referenced anywhere in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` — the reward payout and order removal happen unconditionally.

### Title
`process_delivery_receipt` ignores the decoded `success` flag, paying relayer rewards and settling pending orders for failed Ethereum message dispatches - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge outbound queue v2 pallet decodes a `DeliveryReceipt` from an Ethereum `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` event log, which explicitly carries a `success` boolean indicating whether message execution on Ethereum actually succeeded. This field is verified via merkle/header proof (`T::Verifier::verify`) so it is authentic, but `Pallet::process_delivery_receipt` never inspects `receipt.success` before paying the relayer reward and irrevocably removing the `PendingOrders` entry.

### Finding Description
`DeliveryReceipt` is parsed from the proven Ethereum log with a `success: bool` field [1](#0-0) . The extrinsic `submit_delivery_receipt` verifies the log/proof and decodes the receipt, then calls `process_delivery_receipt` [2](#0-1) .

Inside `process_delivery_receipt`, the code checks the gateway address, looks up the `PendingOrder` by nonce, pays out `order.fee` via `T::RewardPayment::register_reward` whenever `order.fee > 0`, then unconditionally removes the order from `PendingOrders` and emits `MessageDelivered` — at no point is `receipt.success` read or branched on: [3](#0-2) . A repo-wide search confirms the string `success` does not appear anywhere else in this pallet's source, i.e. the decoded flag is discarded entirely.

This is the direct analog of the ERC20 "unchecked return value" bug class: a status/return signal from an external call (here, whether the L1-side dispatch of the bridged command actually succeeded) is obtained but not validated before the pallet advances its own settlement/accounting state (reward payout + queue-entry removal). Per the required pivot — "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — this path violates that invariant: settlement (reward + order removal) advances even when execution demonstrably failed (`success == false`).

### Impact Explanation
Any relayer can submit a delivery receipt for a message whose Ethereum-side execution failed (e.g., the `Command` inside the message reverted on the Gateway contract) and still collect the full relayer fee, because `order.fee > 0` triggers payment regardless of `receipt.success`. Simultaneously, the `PendingOrder` is removed, permanently closing the bookkeeping entry for that nonce — there is no retry/resubmission path once the order is gone. This causes:
- Duplicate/incorrect reward payout for work that did not actually complete (theft of protocol/treasury-sourced relayer rewards).
- Permanent loss of the ability to track or resolve a failed cross-chain command, since the only state (`PendingOrders`) that ties a nonce to a fee is deleted regardless of outcome.

### Likelihood Explanation
This requires no privileged actor, governance action, or malicious relayer collusion beyond an ordinary relayer submitting a legitimately-proven receipt for a message that happened to fail on Ethereum (e.g., due to insufficient gas allotted by `GasMeter`, a reverting command, or any deterministic Ethereum-side execution failure unrelated to the relayer's honesty). Since `success=false` is a normal, expected outcome path in the event log format itself, this is easily triggerable in practice — it does not require an attack, just processing of a receipt for a message that failed to execute.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success == true`. For `success == false`, either keep the `PendingOrder` for a future retry/resend mechanism, or route it to a distinct failure-handling/refund path, and emit a distinguishing event (e.g. `MessageDeliveryFailed`) instead of unconditionally emitting `MessageDelivered` and deleting the order.

### Proof of Concept
1. A message is enqueued via `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0` [4](#0-3) .
2. On Ethereum, the Gateway attempts to dispatch the inbound command and it fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid Merkle/header proof for that log and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the log is authentic), `DeliveryReceipt::try_from` decodes `success: false` into the struct [5](#0-4) .
5. `process_delivery_receipt` is invoked; because `receipt.success` is never checked, `order.fee > 0` still triggers `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, and `PendingOrders::<T>::remove(nonce)` executes unconditionally [6](#0-5) .
6. The relayer is paid for a message that never actually executed on Ethereum, and the nonce's tracking state is gone permanently, matching the existing test pattern for the success case (`submit_delivery_receipt_succeeds_after_unhalt`, which never varies `success`) [7](#0-6) .

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-438)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
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
