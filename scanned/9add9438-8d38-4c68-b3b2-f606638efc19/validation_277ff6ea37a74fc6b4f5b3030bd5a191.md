Based on my investigation, I found a concrete, verifiable analog in the Snowbridge outbound-queue-v2 pallet, matching the report's core defect pattern: a decoded/computed field that is never consulted before an irreversible state transition (payout + settlement) occurs.

### Title
Relayer reward is paid and delivery order is settled without checking `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`process_delivery_receipt` in the Snowbridge V2 outbound queue decodes a `DeliveryReceipt` that carries a `success` flag from the Ethereum `InboundMessageDispatched` event, but the function never reads or checks that flag before paying the relayer reward and irrevocably removing the `PendingOrder`.

### Finding Description
The `DeliveryReceipt` struct decoded from the Ethereum event log explicitly carries a `success: bool` field indicating whether the message dispatch actually succeeded on Ethereum: [1](#0-0) 

However, `process_delivery_receipt`, which is reached from the public, permissionless `submit_delivery_receipt` extrinsic after proof verification, only checks the gateway address and the pending nonce, then unconditionally registers the reward and removes the order — the `success` field of `receipt` is decoded but never referenced anywhere in the function body: [2](#0-1) 

This mirrors the RToken defect exactly: a value that is computed/decoded specifically to gate or scale the final settlement (`index`/`amountScaled` in the RToken report, `success` here) is silently dropped, and the unconditional/unscaled path is taken instead. Here the impact is that a message which *failed* to dispatch on Ethereum (`success == false`) is treated identically to one that succeeded — the fee is still paid out via `T::RewardPayment::register_reward` and the `PendingOrder` is removed, deposit-marking the message as `MessageDelivered`.

Guards that exist (`GatewayAddress` check, `InvalidPendingNonce` check via `PendingOrders::get`) do not stop this path because neither of them examines dispatch outcome — they only validate origin/replay, not success/failure semantics. As a result, an unprivileged relayer can submit a valid Merkle/receipt proof for a message whose execution reverted on Ethereum and still be rewarded, and the order bookkeeping treats it as settled.

### Impact Explanation
This falls under "public underpriced work that degrades ... stalls bridge processing" and "duplicate/incorrect settlement or payout" in the impact gate: relayers are rewarded regardless of whether they actually delivered a working message, and the pending-order state is finalized as if delivery succeeded even when it did not. This can misrepresent bridge delivery status (relying components/consumers of `MessageDelivered` would incorrectly believe execution succeeded) and creates an economic incentive misalignment where the reward budget is drained without requiring correct delivery, which is a genuine breach of "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

### Likelihood Explanation
High likelihood: any relayer who submits a legitimate delivery proof (which they always can, since they only need a real Ethereum receipt for the `InboundMessageDispatched` event, regardless of its `success` value) triggers the payout unconditionally. No malicious peer, governance, or privileged actor is required — this is a public, permissionless entrypoint (`submit_delivery_receipt`) reachable by any signed account whose only precondition is a validly proven event log, not a successful dispatch outcome.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`. For failed deliveries, still remove/settle the `PendingOrder` (or handle retry/failure bookkeeping per intended design) but do not pay the reward, and emit a distinct event (e.g. `MessageDeliveryFailed`) instead of `MessageDelivered` so downstream consumers don't misinterpret failed dispatches as successful.

### Proof of Concept
1. A message is enqueued and processed via `do_process_message`, creating a `PendingOrder { nonce, fee, block_number }` with `fee > 0`: [3](#0-2) 
2. On Ethereum, the message dispatch reverts/fails, and the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer obtains a valid transaction receipt/proof for this event (this is normal, honest relayer behavior — no malicious proof needed) and calls `submit_delivery_receipt` with it.
4. `DeliveryReceipt::try_from` decodes `success = false` correctly: [4](#0-3) 
5. `process_delivery_receipt` is invoked; it never inspects `receipt.success`, so it proceeds to `register_reward(&reward_account, ..., order.fee)` and removes the `PendingOrder`, emitting `Event::MessageDelivered { nonce }` — as confirmed by the existing test flow that only checks gateway-halted and nonce-invalid paths, with no test asserting reward is withheld on `success: false`: [5](#0-4) 

The relayer is paid `order.fee` and the order is settled as "delivered" even though the message execution on Ethereum failed.

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
