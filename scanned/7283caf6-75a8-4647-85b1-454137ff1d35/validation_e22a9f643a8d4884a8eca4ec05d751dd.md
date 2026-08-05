This confirms the finding: `receipt.success` is decoded from the on-chain event but is never read anywhere in `outbound-queue-v2`'s reward-payment path. The pallet doc comment itself states step 10 as "When the message has been verified **and executed**, the relayer will call... to fetch the pending order... pay reward" — but the code never actually verifies execution succeeded, only that the delivery-receipt proof decodes and the gateway address matches.

### Title
Outbound Queue V2 pays relayer reward and clears the pending order regardless of Ethereum-side execution `success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge Outbound Queue V2 pallet registers the relayer reward and permanently removes the `PendingOrders` entry for a nonce based solely on a `DeliveryReceipt` whose `gateway` matches and whose `nonce` exists — it never inspects `receipt.success`, even though that field is decoded directly from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event and exists specifically to signal whether the commands (asset unlocks, mints, contract calls) were actually executed successfully on Ethereum.

### Finding Description
The `DeliveryReceipt` type decodes the `success` bool from the Ethereum event log: [1](#0-0) 

`process_delivery_receipt` only checks the gateway address and that a `PendingOrder` exists for the nonce before unconditionally paying the reward and deleting the order: [2](#0-1) 

`submit_delivery_receipt`, the public extrinsic that any signed relayer can call, verifies the storage/execution proof of the log itself (that the event was really emitted) and decodes it into a `DeliveryReceipt`, then forwards it directly to `process_delivery_receipt` without any success check: [3](#0-2) 

This is functionally identical to the reported Oracle bug: the entity that "fulfils" a request (here, the relayer's `submit_delivery_receipt`) is paid as long as it produces *any* valid receipt for the correct nonce/gateway, without the underlying callback logic (execution of the message's commands on Ethereum, i.e. unlocking/minting tokens or dispatching agent calls) having actually succeeded. The pallet's own doc comment describes the intended flow as "When the message **has been verified and executed**", confirming that success was meant to gate the reward, but the implementation never enforces it: [4](#0-3) 

Because the `PendingOrders` entry is deleted unconditionally on receipt, there is no retry or resend path once a receipt (successful or not) has been submitted for a nonce — the corrupted value is the permanent removal of `PendingOrders<T>` state combined with an unconditional reward payout, both of which occur without checking `receipt.success`.

### Impact Explanation
If the Ethereum Gateway contract's inbound command execution fails (e.g., the message's declared gas budget from `T::GasMeter::maximum_dispatch_gas_used_at_most` is insufficient for the command actually executed, or the command reverts for any other on-chain reason), `InboundMessageDispatched` is still emitted with `success = false`. A relayer can then submit that receipt and be paid the full order fee via `T::RewardPayment::register_reward`, while the intended beneficiary never receives the asset unlock/mint/contract-call effect the message was meant to deliver, and the message can never be resubmitted because its `PendingOrders` entry is gone. This is duplicate/wrongful settlement (reward paid without correct outcome) and a permanent loss of the ability to retry the failed cross-chain action — both squarely within the "duplicate settlement or payout" and "permanent user-fund or bridge-state lock" impact categories.

### Likelihood Explanation
Any relayer (an unprivileged, permissionless role — anyone can call `submit_delivery_receipt`) that observes a `success:false` `InboundMessageDispatched` event (which can occur naturally from underpriced/insufficient committed gas, or could be induced by a relayer choosing a marginal-but-passing gas value on the Ethereum side) can claim the reward. No governance, admin, or validator collusion is required — this matches the "public underpriced work" and "unauthorized... payout" pivots exactly.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying the reward. On `success == false`, either withhold/reduce the reward, keep the `PendingOrders` entry (or move it to a distinct "failed" state) to allow retry/reprocessing or refund of the original sender, and emit a distinct `MessageDeliveryFailed` event instead of `MessageDelivered`.

### Proof of Concept
1. A message with a command whose real Ethereum execution cost exceeds the gas committed via `OutboundCommandWrapper.gas` (computed by `T::GasMeter::maximum_dispatch_gas_used_at_most`) is queued and gets a `PendingOrder` with a non-zero `fee`.
2. The relayer submits the message to the Ethereum Gateway with exactly the committed gas (sufficient to have the transaction itself succeed and the event be emitted, insufficient for the command logic to fully execute), causing `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer calls `submit_delivery_receipt` with a valid proof of that event log.
4. `process_delivery_receipt` (as shown at lines 445-480) checks only `gateway` and nonce existence, pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward`, and removes the `PendingOrders` entry — despite `success == false` and despite the intended recipient never receiving the unlocked/minted asset or the agent call never executing.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L34-41)
```rust
//! 9. On the Ethereum side, the message root is ultimately the thing being verified by the Beefy
//!    light client.
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
