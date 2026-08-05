## Analysis Summary

The external report's core broken invariant is: **state-changing arithmetic/settlement proceeds without verifying that the underlying operation actually completed successfully**, leading to incorrect accounting. Reducing this to a Polkadot SDK analog in the bridge/payout domain, the strongest local match is in the Snowbridge V2 outbound queue's delivery-receipt settlement path, where reward payout state advances **regardless of whether the referenced Ethereum-side message dispatch actually succeeded**.

### Title
Relayer reward payout ignores `DeliveryReceipt.success`, decoupling fee settlement from actual message execution outcome - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` pays out the full `PendingOrder.fee` to the reward account and permanently clears the pending order as soon as *any* verified `InboundMessageDispatched` event log is presented for a known nonce — without inspecting the `success` field of the decoded `DeliveryReceipt`.

### Finding Description
The `DeliveryReceipt` type explicitly carries a `success: bool` field decoded from the `InboundMessageDispatched` Ethereum event log: [1](#0-0) 

However, `process_delivery_receipt` never reads or checks this field. It only validates the gateway address and that a `PendingOrders` entry exists for the nonce, then unconditionally pays the fee and removes the order: [2](#0-1) 

The pallet documentation itself frames the flow as "verify the message with proof ... then pay reward with fee attached in the order" without any conditional on execution success: [3](#0-2) 

This directly violates the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here, payout and pending-order removal (an irreversible settlement action) both advance on decode + gateway match alone, independent of the `success` flag carried in the very receipt that is supposed to attest execution outcome.

### Impact Explanation
Because the `success` field is decoded but discarded, the relayer reward economics are fully decoupled from whether the corresponding commands (e.g., token unlocks/transfers on Ethereum) actually executed. A message whose Ethereum-side commands revert (`success = false`) still causes the pallet to irreversibly clear the `PendingOrders` entry and mint/register the reward. This is exactly the class of "public underpriced work" and "payout state advancing without execution succeeding" that the pivot rules flag as in-scope — reward is settled for work that did not complete, and since `PendingOrders` is removed unconditionally, there is no retry or reconciliation path once a failed receipt has been submitted.

### Likelihood Explanation
This does not require a malicious relayer, validator, or governance actor — it only requires a legitimate, verifiable Ethereum event log (which any relayer can obtain by submitting a message whose commands are crafted to fail on execution, e.g. malformed asset unlock parameters) and a standard `submit_delivery_receipt` call by any signed account. The check `T::Verifier::verify(...)` only attests that the log is real and included on Ethereum — it says nothing about the `success` flag being honored downstream, so any real, honest relayer relaying a genuinely failed dispatch also triggers this path, making it a naturally and repeatably reachable condition rather than a purely theoretical one.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: on failure, either withhold/reduce the fee reward, re-queue/retry the underlying commands, or route the fee to a distinct failure-handling path instead of paying full price to the relayer and permanently discarding the order. At minimum, emit a distinguishable event (e.g. `MessageDeliveryFailed`) and gate `T::RewardPayment::register_reward` on `receipt.success == true`.

### Proof of Concept
1. A sender submits an outbound V2 message via `send_message_impl` whose command is crafted to fail on the Ethereum gateway (e.g., an asset unlock to an address/contract that reverts, or a command exceeding execution constraints) — this is accepted into `PendingOrders` with a nonzero `fee` at `do_process_message`: [4](#0-3) 
2. The relayer (which can be the sender itself, or any signed account) relays the message; on Ethereum the command reverts, and the Gateway contract still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer submits `submit_delivery_receipt` with a valid inclusion proof for this real (but `success=false`) log.
4. `T::Verifier::verify` succeeds (the log is genuinely on-chain), `DeliveryReceipt::try_from` decodes `success=false` but `process_delivery_receipt` ignores it, pays the full `order.fee` to `reward_account`, and removes the `PendingOrders` entry — settlement completes as if the message had executed successfully: [5](#0-4)

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
