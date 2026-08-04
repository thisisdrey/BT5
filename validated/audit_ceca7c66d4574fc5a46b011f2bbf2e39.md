## Finding

### Title
`process_delivery_receipt()` ignores the decoded `DeliveryReceipt.success` flag, unconditionally paying relayer rewards and permanently discarding failed message orders - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` (which carries a `success: bool` field taken directly from the Ethereum `InboundMessageDispatched` event) but never reads or branches on that field before paying the relayer reward and deleting the corresponding `PendingOrder`.

### Finding Description
The Ethereum-side `InboundMessageDispatched` event explicitly encodes whether the dispatched command actually succeeded on Ethereum: [1](#0-0) 

This `success` field is decoded into the `DeliveryReceipt` struct, but `Pallet::process_delivery_receipt` never inspects it. It only validates the gateway address, looks up the `PendingOrder` by nonce, pays the fee, and unconditionally removes the order: [2](#0-1) 

`submit_delivery_receipt` (the public, unprivileged, signed extrinsic) is the only caller of this logic — after verifying the Merkle/light-client proof of the log via `T::Verifier::verify`, it decodes the receipt and forwards straight into `process_delivery_receipt` with no additional gating on `success`: [3](#0-2) 

Per the module documentation, `submit_delivery_receipt` is meant to attest that "the message has been verified **and executed**" before rewarding the relayer and clearing the pending order: [4](#0-3) 

However, the implementation settles the order the same way whether `success` is `true` or `false`. Any relayer can legitimately obtain a real, correctly-proven event log where the Gateway's command execution reverted on Ethereum (e.g. an asset-unlock/mint command that failed due to destination-side conditions) and submit that proof: verification passes (it's a real emitted event), decoding passes, and the reward is paid and the order is deleted exactly as in the success case.

### Impact Explanation
This breaks the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Two concrete harms follow directly from the missing check:
1. Relayers are paid the full fee for delivering messages whose commands failed on Ethereum — public underpriced/incorrect work is rewarded as if correctly settled.
2. `PendingOrders::<T>::remove(nonce)` permanently discards the order regardless of outcome, and `Event::MessageDelivered` is emitted even though the command failed. There is no retry, refund, or compensation path in this pallet for a `success = false` outcome, so any assets whose finalization depended on that command's execution on Ethereum become permanently unrecoverable/stuck — a bridge-state lock impact.

### Likelihood Explanation
No special privileges are required. Any account can call the public `submit_delivery_receipt` extrinsic with a legitimately verifiable receipt whose `success` field is `false` — this is an ordinary occurrence (e.g. gas griefing or state changes on Ethereum making the dispatched command revert), not requiring a malicious relayer, validator, or admin action; it is inherent to the pallet's current logic.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: on failure, do not (or only partially) reward the relayer, and do not unconditionally clear/finalize the `PendingOrder` as delivered — instead route it to a distinct failure/retry state (or explicit compensation flow) so bridge settlement only finalizes after confirmed successful execution.

### Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }`.
2. On Ethereum, the Gateway processes the message but the corresponding command execution reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer builds `EventProof` for this real event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (proof is valid), `DeliveryReceipt::try_from` succeeds with `success=false`.
5. `process_delivery_receipt` pays `order.fee` to `reward_account` and removes `PendingOrders[nonce]`, emitting `MessageDelivered`, identical to what happens for a genuinely successful delivery — confirmed by the existing test `invalid_nonce_for_delivery_receipt_fails` and `poc_m1` in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs`, none of which exercise or assert behavior for `success = false` in `process_delivery_receipt`.

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
