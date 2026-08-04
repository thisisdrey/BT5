## Finding: Relayer reward is paid unconditionally in Snowbridge outbound-queue-v2, ignoring the `success` field of the delivery receipt

### Title
Relayer reward settles regardless of message dispatch outcome on Ethereum - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is: *a signed/attested payload is accepted and used to drive downstream settlement without verifying that its content actually corresponds to, and confirms the intended outcome of, the original request.* The local analog is in `Pallet::process_delivery_receipt` in Snowbridge's outbound-queue-v2 pallet. The `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event log carries an explicit `success: bool` field indicating whether the message dispatch on Ethereum actually succeeded, but this field is never inspected before the relayer reward is paid and the `PendingOrder` is settled.

### Finding Description
`DeliveryReceipt` is defined with a `success` field decoded straight from the Ethereum event log: [1](#0-0) 

`process_delivery_receipt` reads the `DeliveryReceipt`, checks only the `gateway` address and the existence of the corresponding `PendingOrders` entry by `nonce`, then unconditionally pays the reward and removes the order: [2](#0-1) 

Note that `receipt.success` is never read anywhere in this function, and `PendingOrder` itself only stores `nonce`, `fee`, and `block_number` (per the module documentation), so the only binding between the receipt and the original message is the bare `nonce` — no `topic`/content hash cross-check is performed either: [3](#0-2) 

This mirrors the report's exploit shape exactly: a piece of externally-verified, cryptographically legitimate data (a real, proof-verified event log, analogous to a validly-signed dealing support) has fields that describe the true outcome of the underlying operation (`success`), yet the settlement logic (`process_delivery_receipt` / `confirm_delivery`-equivalent) advances payout state without checking that field, i.e. without checking that the "signed" outcome matches the intended completed operation.

### Impact Explanation
This breaks the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Because `success` is ignored:
- A relayer can submit (or simply encounter) a transaction on Ethereum where the `Gateway.submitDelivery`/dispatch of the underlying command reverts or fails (e.g., insufficient gas supplied for the inner command, a legitimately-reverting command), yet the wrapping `InboundMessageDispatched` event is still emitted with `success = false`.
- The relayer then submits that proof via `submit_delivery_receipt`. `process_delivery_receipt` pays out `order.fee` and clears the `PendingOrder` exactly as if the message had been successfully delivered.
- This results in the bridge paying out relayer rewards for message deliveries that did not actually complete the intended cross-chain effect — a form of underpriced/incorrect settlement that drains the reward pool without the corresponding service being rendered, and without any privileged actor, malicious validator, or leaked key involved (any relayer can trigger this simply by controlling their own Ethereum-side gas/tx parameters, which is not a "malicious peer/relayer" assumption in the excluded sense — this is a design gap in a public, permissionless extrinsic).

### Likelihood Explanation
Likelihood is straightforward: this requires only a normal relayer performing its normal duty of calling `submit_delivery_receipt` with a legitimately obtained (not forged) receipt proof where the underlying dispatch failed. No cryptographic forgery, collusion, or governance/admin action is needed — the vulnerability is purely a missing check (`ensure!(receipt.success, ...)`) in a public entrypoint.

### Recommendation
In `process_delivery_receipt`, add an explicit check on `receipt.success` before crediting `T::RewardPayment::register_reward`. If `success == false`, either withhold/reduce the reward (e.g., pay only a smaller "confirmation" fee, matching the `bridges/modules/messages` pattern where delivery reward is tied to actually-confirmed messages) or reject the receipt so the order remains pending for retry, rather than settling it as fully paid and removing it from `PendingOrders`. Additionally, consider binding the receipt to the original message content (e.g., `topic`) rather than trusting `nonce` alone, to fully close the "signed content vs. original content" gap identified in the source report.

### Proof of Concept
1. A message is enqueued via `do_process_message`, creating `PendingOrders[nonce] = { fee, .. }`. [4](#0-3) 
2. The relayer submits the message to the Ethereum Gateway but arranges for the inner command dispatch to fail (e.g., supplies gas just enough for the outer call to log the event but insufficient for the command execution), causing the contract to emit `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer obtains a valid Merkle/receipt proof of this real (not forged) event and calls `submit_delivery_receipt` on BridgeHub.
4. `process_delivery_receipt` decodes the receipt, checks `gateway` and `PendingOrders::get(nonce)`, and — because `receipt.success` is never checked — pays the full `order.fee` reward and removes the order: [5](#0-4) 
5. Result: the relayer is fully rewarded even though the outbound message was never successfully dispatched on Ethereum, confirming the missing "success" validation gap.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L20-41)
```rust
//!    [`frame_support::traits::ProcessMessage::process_message`]
//! 5. The message is processed in `Pallet::do_process_message`:
//! 	a. Convert to `OutboundMessage`, and stored into the `Messages` vector storage
//! 	b. ABI-encode the `OutboundMessage` and store the committed Keccak256 hash in `MessageLeaves`
//! 	c. Generate `PendingOrder` with assigned nonce and fee attached, stored into the
//! 	   `PendingOrders` map storage, with nonce as the key
//! 	d. Increment nonce and update the `Nonce` storage
//! 6. At the end of the block, a merkle root is constructed from all the leaves in `MessageLeaves`.
//!    At the beginning of the next block, both `Messages` and `MessageLeaves` are dropped so that
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
