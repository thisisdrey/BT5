## Analysis

The external report's core invariant is: **a boolean success/status value returned from an external call must be checked before the caller proceeds with state changes that assume success.**

A direct local analog exists in the Snowbridge outbound queue v2 delivery-receipt processing flow.

### Where it happens

`DeliveryReceipt` is decoded from an Ethereum `InboundMessageDispatched` event log and explicitly carries a `success: bool` field indicating whether the message actually executed successfully on the Ethereum side: [1](#0-0) 

That `success` field is populated from the decoded event log: [2](#0-1) 

But `Pallet::process_delivery_receipt`, which is invoked from the public extrinsic `submit_delivery_receipt`, never reads or checks `receipt.success`. It only checks the gateway address and the existence of a `PendingOrders` entry for the nonce, then unconditionally pays the relayer reward and removes the order: [3](#0-2) 

The public entrypoint that reaches this code, after verifying only the merkle/receipt *proof* (not the semantic outcome), is: [4](#0-3) 

A search across the pallet confirms `receipt.success` (or any `.success` check) is referenced only at decode time in `delivery_receipt.rs` and nowhere in `outbound-queue-v2/src/lib.rs`'s reward-payout logic, so the value is effectively discarded — the exact "return value not processed" pattern from the ERC-20 report, just applied to a bridge delivery-status flag instead of a token-transfer boolean.

### Title
Unchecked `DeliveryReceipt.success` flag allows relayer reward payout for failed message execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`process_delivery_receipt` decodes a `DeliveryReceipt` (via `DeliveryReceipt::try_from`) that includes a `success: bool` field reporting whether the corresponding message actually executed successfully on Ethereum. The pallet verifies the cryptographic proof of the event log, but never inspects `receipt.success` before paying the pending relayer reward, so proofs of *failed* executions are treated identically to proofs of successful ones for reward purposes.

### Finding Description
The `submit_delivery_receipt` extrinsic verifies event-log authenticity via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt`, and calls `process_delivery_receipt`. That function checks only `receipt.gateway` and looks up `PendingOrders` by `receipt.nonce`; it pays `order.fee` to the reward account and removes the order unconditionally whenever those two checks pass — `receipt.success` is decoded but discarded. [5](#0-4)  This mirrors the ERC-20 pattern where a boolean success return is produced by an operation but the caller never checks it before proceeding with dependent logic (crediting a payout).

### Impact Explanation
Because the Ethereum-side execution status is never validated, a relayer can submit a proof for a genuine `InboundMessageDispatched(nonce, topic, success=false, reward_address)` event — i.e., a message whose execution on Ethereum actually failed/reverted — and still collect the full relayer fee from `PendingOrders`, and the order is removed as if delivery succeeded. This is an unbacked/duplicate-condition reward payout: fee funds are settled to a relayer for work that did not achieve its intended outcome, and legitimate future re-delivery or reconciliation of the failed message loses its associated fee since the order is deleted. This falls under "bridge rewards ... must conserve value and settle exactly once to the rightful beneficiary and amount" / "duplicate settlement or payout."

### Likelihood Explanation
No privileged role is required — any unpermissioned account can call `submit_delivery_receipt` (`ensure_signed(origin)?`) with a genuinely valid proof for a `success=false` event that naturally occurs whenever the destination-side call reverts (e.g., gas griefing, execution errors) on the Ethereum gateway. No malicious relayer/validator/prover collusion is needed — the attacker merely submits the truthful proof of a failed delivery to claim the reward anyway.

### Recommendation
In `process_delivery_receipt`, explicitly branch on `receipt.success`: only pay `order.fee` and treat delivery as settled when `success == true`; for `success == false`, either withhold the reward, requeue/allow re-relay, or otherwise avoid deleting the `PendingOrders` entry as if delivery had succeeded.

### Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }` with `fee > 0`. [6](#0-5) 
2. On Ethereum, the corresponding call to the Gateway executes but reverts/fails, so the emitted `InboundMessageDispatched` event has `success = false`.
3. Any signed relayer submits `submit_delivery_receipt` with a valid inclusion proof of that event.
4. `T::Verifier::verify` succeeds (proof is genuine), `DeliveryReceipt::try_from` decodes `success = false` into the struct, but `process_delivery_receipt` never inspects it — the reward is paid via `T::RewardPayment::register_reward` and the order removed, exactly as in the passing `submit_delivery_receipt_succeeds_after_unhalt` test flow which never asserts on `success`. [7](#0-6)

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
