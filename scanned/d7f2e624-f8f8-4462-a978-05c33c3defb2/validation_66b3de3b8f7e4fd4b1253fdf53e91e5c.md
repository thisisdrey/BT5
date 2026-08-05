Based on my investigation, `DeliveryReceipt` from the `InboundMessageDispatched` Ethereum event carries an explicit `success: bool` field [1](#0-0) , but `Pallet::process_delivery_receipt` in `outbound-queue-v2` never reads or checks that field before paying the relayer reward.

### Title
Relayer reward paid regardless of message execution outcome due to unchecked `DeliveryReceipt.success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
### Finding Description
`process_delivery_receipt` decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` that includes a `success` flag indicating whether the message actually executed successfully on Ethereum [1](#0-0) . However, the pallet's handler only validates the gateway address and the pending nonce, then unconditionally pays the fee to the relayer and removes the order — it never inspects `receipt.success`: [2](#0-1) 

The pipeline doc even documents the intended behavior as "Fetch the pending order by nonce of the message, pay reward with fee attached in the order" without mentioning any success check [3](#0-2) . This is the FRAME-side analog of the CosmWasm bug's core invariant break: a settlement path that finalizes payout state without conditioning it on the actual success of the underlying operation. In the anchor report the missing condition caused funds to lock; here the missing condition causes funds (reward) to be paid out even when the corresponding cross-chain execution failed, and simultaneously the `PendingOrders` entry is deleted — permanently closing the door on any corrective accounting for that nonce.

### Impact Explanation
Any relayer can submit a valid delivery-receipt proof for a message whose execution on Ethereum failed (`success: false`) and still collect the reward fee, because `T::RewardPayment::register_reward` is invoked unconditionally on `order.fee > 0` [4](#0-3) . This breaks the intended settlement invariant (reward should be paid only for correctly delivered/executed messages), causing over-payment of reward funds from the fee pool without correct binding to the delivery outcome, and the order is removed so there is no path to reconcile or reclaim on failure — the fee is unbacked-mint-like leakage from the relayer reward pool.

### Likelihood Explanation
The failure path (`success: false`) is a legitimate, permissionless-to-observe outcome on Ethereum (e.g., gas exhaustion, revert in the gateway's command execution, insufficient balance for the operation being relayed) and requires no privileged actor — any relayer holding a valid Ethereum receipt/proof for a failed dispatch can call `submit_delivery_receipt` and be rewarded exactly as if it had succeeded. No malicious peer, validator, or governance action is required; the verifier (`T::Verifier::verify`) only attests that the log is authentic, not that `success == true`.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success` is `true`; on `false`, either emit a distinct `MessageDeliveryFailed` event while still removing the order (if fee should be forfeited/refunded elsewhere) or explicitly define what happens to the fee (e.g., return it to the sender instead of paying the relayer), rather than silently treating both outcomes identically.

### Proof of Concept
1. A message is queued and gets a `PendingOrder { nonce, fee, .. }` entry via `do_process_message` [5](#0-4) .
2. On Ethereum, the gateway's command execution for that nonce reverts/fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds an `EventProof` for that failed-execution log and calls `submit_delivery_receipt` [6](#0-5) .
4. `process_delivery_receipt` verifies the proof, decodes `success: false` into the `DeliveryReceipt`, but proceeds to `register_reward` for `order.fee` anyway and deletes the `PendingOrders` entry [7](#0-6) .
5. The relayer is rewarded for a message that never actually executed successfully on the destination chain, and no state remains to detect or reverse this.

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
