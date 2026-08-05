### Title
Outbound delivery receipt pays relayer rewards for failed Ethereum executions - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The Snowbridge outbound-queue-v2 pallet decodes the Ethereum `InboundMessageDispatched` event's `success` boolean into `DeliveryReceipt.success`, but `process_delivery_receipt` never reads it. A valid delivery proof for a message that reverted on Ethereum is therefore settled exactly like a successful one: the relayer reward is registered, the `PendingOrder` is removed, and the message is marked delivered. This mirrors the external bug class: the pallet advances state using the "message appeared in a receipt" value instead of the "message executed successfully" value.

### Finding Description
The `DeliveryReceipt` struct parsed from the Ethereum event log carries a `success` field. [1](#0-0)  The event signature is `event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);`, so the receipt explicitly distinguishes successful from reverted dispatches. [2](#0-1) 

In `process_delivery_receipt`, the pallet verifies `receipt.gateway`, resolves `reward_account`, loads the `PendingOrder` by `nonce`, and if `order.fee > 0` calls `T::RewardPayment::register_reward` and removes the order from `PendingOrders`. [3](#0-2)  The `success` field is never inspected anywhere in that function. [4](#0-3) 

The pallet overview documents that `submit_delivery_receipt` should run only "when the message has been verified and executed" and then pay the reward and remove the order. [5](#0-4)  The implementation enforces "verified" through the verifier and `gateway` check, but it does not enforce "executed successfully".

### Impact Explanation
Because `receipt.success` is ignored, a relayer can be paid for a message that failed on Ethereum. The fee attached to the `PendingOrder` is transferred to the `reward_address` even though no successful cross-chain action occurred. [6](#0-5)  At the same time, the `PendingOrder` is deleted, so the original sender cannot retry the message or reclaim the fee. [7](#0-6)  This is an unauthorized payout with wrong beneficiary/amount semantics and corrupts the bridge's delivery-settlement state.

### Likelihood Explanation
High. The entrypoint is public and signed by any relayer. The Ethereum Gateway emits `InboundMessageDispatched` for reverted calls with `success=false`, and a valid beacon receipt proof of such an event will pass the verifier. The pallet then processes it without any additional success check, so the attack requires only submitting a valid proof for a failed message and setting `reward_address` to an attacker-controlled account.

### Recommendation
In `process_delivery_receipt`, require `receipt.success == true` before registering the reward and removing the `PendingOrder`. For `success == false`, leave the order in place (or route the fee to a refund/retry path according to bridge policy) and emit a distinct failure event instead of `MessageDelivered`.

### Proof of Concept
1. A user sends an outbound Snowbridge v2 message with fee `F`, creating `PendingOrder { nonce: N, fee: F }`. [8](#0-7) 
2. On Ethereum the message call reverts; the Gateway emits `InboundMessageDispatched(nonce=N, topic=..., success=false, reward_address=attacker)`. [9](#0-8) 
3. The attacker relays the valid receipt proof to `submit_delivery_receipt` on BridgeHub. The verifier accepts the proof because the event is contained in a valid Ethereum receipt. [10](#0-9) 
4. `process_delivery_receipt` checks `gateway`, loads `PendingOrders[N]`, and because `order.fee > 0` calls `register_reward(&attacker, ..., F)` and removes the order. [3](#0-2) 
5. The attacker claims `F`. The sender loses the fee and the failed message cannot be retried because `PendingOrders[N]` no longer exists.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L29-42)
```rust
//!    state at each block only holds the messages processed in that block.
//! 7. This merkle root is inserted into the parachain header as a digest item
//! 8. Offchain relayers are able to relay the message to Ethereum after:
//! 	a. Generating a merkle proof for the committed message using the `prove_message` runtime API
//! 	b. Reading the actual message content from the `Messages` vector in storage
//! 9. On the Ethereum side, the message root is ultimately the thing being verified by the Beefy
//!    light client.
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
//!
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
