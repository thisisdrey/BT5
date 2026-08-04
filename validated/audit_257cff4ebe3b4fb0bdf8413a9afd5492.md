### Title
`process_delivery_receipt` pays relayer reward without checking dispatch success or message topic - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's broken invariant is: a value that is supposed to bind a signature/settlement action to the *actual* outcome of an operation is replaced by an empty/default placeholder, so the settlement step proceeds even though the real-world data it should be conditioned on was never validated. The local analog is `snowbridge_pallet_outbound_queue_v2::Pallet::process_delivery_receipt`, which pays out the relayer reward and clears the `PendingOrder` for a nonce based only on `gateway` and `nonce` matching, while ignoring the `receipt.success` and `receipt.topic` fields decoded from the same `DeliveryReceipt`.

### Finding Description
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field indicating whether the outbound command was actually executed successfully by the Gateway contract on Ethereum, and a `topic: H256` tying the receipt to the specific message content: [1](#0-0) 

`submit_delivery_receipt` verifies the cryptographic proof of the log and decodes the receipt, then calls `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` only checks `receipt.gateway` against the configured gateway address and looks up the `PendingOrder` by `receipt.nonce`. It never reads `receipt.success` or `receipt.topic`: [3](#0-2) 

The pallet doc explicitly states the intended flow requires verifying "the message with proof for a transaction receipt containing the event log" and then paying "reward with fee attached in the order" — implying the receipt's execution outcome is meant to gate reward settlement: [4](#0-3) 

Because `success` and `topic` are decoded but discarded, any legitimately-proven `InboundMessageDispatched` event for the correct nonce settles the `PendingOrder` and pays the fee to `reward_address` regardless of whether the corresponding command actually executed correctly on Ethereum, and regardless of whether the topic matches the message that was actually committed for that nonce. This mirrors the underlying class of bug in the external report: a field required to correctly bind settlement to the real state of the operation is effectively treated as an empty/unchecked placeholder, so the pallet's guard ("only pay if delivery succeeded") does not actually exist in code even though the data needed to enforce it is present.

### Impact Explanation
This breaks the invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A relayer can submit a valid, cryptographically-proven delivery receipt for a message whose on-chain (Ethereum-side) execution failed (`success == false`) and still collect the full relay fee, and the `PendingOrder` is removed as if delivery fully succeeded. This is an unbacked/duplicate-style payout: the fee is meant to reward *successful* delivery, but is paid for failed delivery too, draining bridge fee reserves without the corresponding service being rendered. It also means `Event::MessageDelivered` is emitted for messages whose commands never actually executed, corrupting downstream observability/accounting of bridge delivery state.

### Likelihood Explanation
Any relayer (an unprivileged, permissionless actor — anyone can call `submit_delivery_receipt`) can trigger this by relaying a legitimate proof for a message whose Ethereum-side command execution reverted/failed but whose transaction still emitted the `InboundMessageDispatched` log with `success = false`. No malicious peer, validator, governance action, or leaked key is required — only a normal relayer submitting a real (not forged) proof, which the pallet's own verifier accepts as valid.

### Recommendation
In `process_delivery_receipt`, explicitly check `receipt.success` before calling `T::RewardPayment::register_reward`, and consider also validating `receipt.topic` against the topic recorded when the `PendingOrder`/message was queued (e.g., store the topic alongside the order, or thread it through `Messages`) so an unsuccessful or mismatched delivery cannot silently settle payout.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrder { nonce, fee, .. }` with `fee > 0`. [5](#0-4) 
2. On Ethereum, the Gateway contract processes the message but the command execution fails; the contract still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid receipt/execution proof for this real log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the log/proof is genuine), `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` never inspects it — the `order.fee` is paid via `T::RewardPayment::register_reward`, and `PendingOrders` is removed, exactly as in the success case. [6](#0-5) 

Note: I could not find a git history or PR discussion in this index confirming whether `success`-gating was intentionally omitted as a design choice (e.g., relayers might be paid purely for the "relay" service regardless of downstream execution outcome) or is an outright bug; this uncertainty should be resolved by checking the pallet's design docs/PR discussion before treating this as a confirmed vulnerability rather than an intentional design decision.

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
