### Title
`process_delivery_receipt` pays relayer reward without checking `DeliveryReceipt.success`, allowing payout for failed message delivery - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`snowbridge_pallet_outbound_queue_v2::Pallet::process_delivery_receipt` decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` struct that includes a `success: bool` field indicating whether the outbound message actually executed successfully on Ethereum. That field is decoded correctly by `DeliveryReceipt::try_from`, but `process_delivery_receipt` never reads or checks it before paying the relayer's reward. This is structurally the same bug class as the reported `_swap()` issue: a value that is supposed to gate/validate an outcome (`rdntOut`, here `receipt.success`) is computed/decoded but never actually consulted, so the guard it should provide never fires.

### Finding Description
The `DeliveryReceipt` struct explicitly carries a `success` flag decoded from the on-chain Ethereum event: [1](#0-0) 

The Solidity event itself is defined with a `success` parameter and is faithfully decoded into the Rust struct: [2](#0-1) 

However, `process_delivery_receipt`, which is the function invoked by the public extrinsic `submit_delivery_receipt` after the Ethereum receipt proof is verified, only checks `receipt.gateway` and looks up the `PendingOrder` by `receipt.nonce`. It unconditionally pays the fee and removes the order — `receipt.success` is never referenced: [3](#0-2) 

The extrinsic's own doc-comment describes the intended flow as "fetch the pending order by nonce ... pay reward" without any mention of checking delivery status, confirming the check is simply absent from the design, not filtered out elsewhere: [4](#0-3) 

Nowhere in `bridges/snowbridge/pallets/outbound-queue-v2` or the shared `snowbridge_verification_primitives::Verifier::verify` path is `success` inspected — `grep` for `.success` usage in the whole `bridges/snowbridge` tree only turns up the field definition itself, not any consumer: [5](#0-4) 

Because `Verifier::verify` only proves that the log/event existed and was included in a finalized block — it says nothing about whether the *contents* of that event (the `success` flag) are consulted by the caller — a legitimately-included `InboundMessageDispatched(nonce, topic, success=false, reward_address)` event (i.e., a message that reverted/failed execution on the Gateway contract on Ethereum) will still cause `process_delivery_receipt` to pay the relayer fee and remove the pending order, exactly as if delivery had succeeded.

### Impact Explanation
This breaks the intended invariant that relayer rewards settle only after successful execution/dispatch of the bridged message (as stated in the pallet's own design doc, `pay reward with fee attached in the order` is meant to gate on successful delivery, mirrored by the field's very name `success`). A relayer can be rewarded for delivering a message that failed on the Ethereum side, since any real Ethereum `InboundMessageDispatched` log with `success=false` is a legitimate, provable event — no forged proof is needed. This is a public-underpriced/incorrect-payout issue matching "duplicate settlement or payout" / conserving-value in the impact gate: BridgeHub pays out fee from `PendingOrders` regardless of whether the corresponding cross-chain effect actually occurred, and the `PendingOrder` is removed either way, so there is no future correction path (state cannot "retry" after removal).

### Likelihood Explanation
High. No malicious peer, relayer, validator, or governance action is required — an unprivileged relayer submits `submit_delivery_receipt` with a real signed/verifiable Ethereum receipt proof for any message whose execution failed on the Gateway contract (e.g., due to insufficient gas, revert in downstream XCM execution, etc.), which is a normal occurrence, not an attack precondition. The existing `Verifier::verify`/`verify_receipt_proof` checks (chain/halted-state, receipt inclusion, tx index) are all satisfied by a genuine failed-delivery event; none of them are designed to check the semantic `success` field, so they do not block this path.

### Recommendation
Add an explicit check on `receipt.success` in `process_delivery_receipt` before paying the reward:
```rust
ensure!(receipt.success, Error::<T>::DeliveryFailed);
```
placed before the `T::RewardPayment::register_reward` call, so that reward payout and `PendingOrders` removal only occur for successful deliveries. Consider a separate code path for failed deliveries (e.g., retry, refund `fee` to the original sender, or an explicit "message failed" event) rather than silently discarding the failure signal.

### Proof of Concept
1. A message is queued and `PendingOrders` holds an order with `nonce = N`, `fee = F`.
2. The message is delivered to the Ethereum Gateway contract, but execution fails on Ethereum (e.g., XCM decode/dispatch fails on the Ethereum side or downstream contract call reverts), so the Gateway emits `InboundMessageDispatched(nonce = N, topic, success = false, reward_address)`.
3. A relayer builds a valid receipt-inclusion proof for this real (unforged) Ethereum event, exactly as in the existing test flow shown by `submit_delivery_receipt_succeeds_after_unhalt`: [6](#0-5) 
4. Relayer calls `submit_delivery_receipt(origin, event)`. `Verifier::verify` succeeds (the proof and log are genuinely valid), `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` ignores this and pays `order.fee` to `reward_account`, then removes the order — identical outcome to a successful delivery.
5. Expected/correct behavior: the extrinsic should reject or otherwise not pay the reward when `success == false`.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
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

**File:** bridges/snowbridge/pallets/ethereum-client/src/impls.rs (L15-41)
```rust
impl<T: Config> Verifier for Pallet<T> {
	/// Verify a message by verifying the existence of the corresponding
	/// Ethereum log in a block. Returns the log if successful. The execution header containing
	/// the log is sent with the message. The beacon header containing the execution header
	/// is also sent with the message, to check if the header is an ancestor of a finalized
	/// header.
	fn verify(event_log: &Log, proof: &Proof) -> Result<(), VerificationError> {
		// Refuse to verify any Ethereum-side proof while the beacon light client is halted.
		// Governance halts the light client when it suspects a compromise (e.g. sync committee
		// takeover), at which point any signed headers/receipts must be treated as untrusted.
		// Covers every Verifier consumer, including `inbound_queue_v2::submit` and
		// `outbound_queue_v2::submit_delivery_receipt` (which would otherwise still drain
		// pending relayer rewards while the bridge is halted).
		ensure!(!Self::operating_mode().is_halted(), VerificationError::Halted);

		Self::verify_execution_proof(&proof.execution_proof)
			.map_err(|e| InvalidExecutionProof(e.into()))?;

		Self::verify_receipt_inclusion(
			proof.execution_proof.execution_header.receipts_root(),
			event_log.tx_index,
			&proof.receipt_proof,
			event_log,
		)?;

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
