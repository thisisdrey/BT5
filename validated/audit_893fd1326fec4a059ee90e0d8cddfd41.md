## Analysis

The Sherlock report's core defect is that a reward-payout function used the **wrong on-chain state field** to determine whether a rewardable action had actually happened as expected, so rewards were paid based on state that doesn't reflect true "did this happen correctly on this chain" semantics.

The closest verifiable local analog is in Snowbridge's outbound queue v2 pallet, where the relayer reward payout ignores the `success` field of the delivery receipt.

### Title
Snowbridge `OutboundQueueV2::process_delivery_receipt` pays relayer reward without checking dispatch `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event explicitly carries a `success: bool` field indicating whether the outbound command actually executed successfully on the Gateway contract. [1](#0-0) 
However, `Pallet::process_delivery_receipt` never reads or checks `receipt.success` before releasing the reward tied to the `PendingOrder`; it only validates `gateway` and looks up the order by `nonce`. [2](#0-1) 

### Finding Description
The pallet doc explicitly states the intended flow: "When the message has been verified and executed, the relayer will call ... `submit_delivery_receipt` to ... pay reward with fee attached in the order." [3](#0-2) 
The intent is that reward is tied to successful execution ("has been verified **and executed**"). But the implementation only checks the Merkle/receipt proof (that the event log genuinely came from the Gateway) via `T::Verifier::verify`, decodes the receipt, and then pays out solely based on `order.fee > 0`:
```rust
let reward_account = if receipt.reward_address == [0u8; 32] { relayer } else { receipt.reward_address.into() };
let nonce = receipt.nonce;
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
``` [4](#0-3) 
`receipt.success` is decoded from the log but is a dead field on this path — a grep across the pallet confirms it is only referenced in the primitive struct definition, never in the pallet logic that gates the reward.

This mirrors the Lend bug's root cause exactly: a payout function relies on an easily-available but semantically-wrong signal (here, "a receipt with this nonce exists and its Merkle proof is valid" instead of "the command associated with this nonce actually executed successfully on Ethereum") rather than the correct discriminator (`success`) that the protocol itself defines and emits for this exact purpose.

### Impact Explanation
Any relayer can submit a valid Merkle/receipt proof for a message whose dispatch failed on the Ethereum Gateway (e.g., due to insufficient gas supplied by the relayer, a reverted command, or any other execution failure that still produces an `InboundMessageDispatched(success=false)` log) and still collect the full `order.fee` reward, exactly as if the message had been successfully delivered and executed. This is a public, underpriced/over-rewarded work path: relayers are incentivized to submit minimally-gassed or failing deliveries just to claim the fee, degrading the intended "pay-for-successful-work" guarantee of the bridge's economic model and causing funds (fees pre-paid by senders in `PendingOrders`) to be paid out to relayers for work that was not actually completed on the destination chain.

### Likelihood Explanation
This requires no privileged actor, malicious relayer collusion assumption changes, or governance action — an ordinary permissionless relayer (the same actor already expected to call `submit_delivery_receipt`) can trigger this by simply relaying real (valid, provable) but execution-failed events, which are a normal occurrence on any EVM chain (e.g., out-of-gas). The existing proof verification (`T::Verifier::verify`) correctly authenticates the receipt content but does nothing to gate on `success`, so this guard does not stop the path.

### Recommendation
In `process_delivery_receipt`, only register the reward when `receipt.success == true`; for `success == false` receipts, remove the `PendingOrder` without payout (or route to a distinct, smaller "attempted delivery" compensation path if that is desired), matching the "executed" language in the pallet's own doc comment.

### Proof of Concept
1. A message with `fee > 0` is queued via `do_process_message`, creating `PendingOrders[nonce] = { fee, ... }`. [5](#0-4) 
2. The relayer submits the message to the Ethereum Gateway with just enough gas for the transaction to be included but insufficient for the command to actually execute (or the command reverts for any reason). Ethereum emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer calls `submit_delivery_receipt` with a valid proof of this event. `T::Verifier::verify` succeeds (the event is real), `DeliveryReceipt::try_from` decodes `success=false`. [6](#0-5) 
4. `process_delivery_receipt` proceeds to `register_reward(&reward_account, ..., order.fee)` unconditionally since `order.fee > 0`, paying out the full fee despite `success == false`. [7](#0-6) 
5. `PendingOrders[nonce]` is removed, so the sender's fee is permanently and irrevocably spent rewarding a failed execution.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L300-317)
```rust
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
