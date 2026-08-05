The `success` field on `DeliveryReceipt` is decoded from the Ethereum event log but never referenced anywhere in `bridges/snowbridge/pallets/outbound-queue-v2/`. This confirms the analog: the boolean success indicator is completely dropped from the logic path that pays rewards.

### Title
Relayer reward paid and order settled regardless of `DeliveryReceipt.success` boolean in `process_delivery_receipt` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`process_delivery_receipt` decodes an Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` struct that carries a `success: bool` field indicating whether the message dispatch on Ethereum actually succeeded. This mirrors the reported pattern of an underlying call returning a boolean success indicator (`withdrawAndUnwrap(...) returns(bool)`) that the caller must check before proceeding with dependent state changes. In this pallet, the `success` value is parsed but never consulted before the relayer reward is registered and the pending order is removed.

### Finding Description
`DeliveryReceipt` is decoded from the Solidity event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` and explicitly stores `success` as a field: [1](#0-0) 

`process_delivery_receipt` uses `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` to validate and settle the order, pay out the fee via `T::RewardPayment::register_reward`, and remove the entry from `PendingOrders`, but `receipt.success` is never read: [2](#0-1) 

A grep across the entire `outbound-queue-v2` pallet directory for the string `success` returns zero matches, confirming the field is decoded and then discarded — it is not used in any `ensure!`, event, or state-transition guard anywhere in this pallet's business logic.

The dispatch entrypoint `submit_delivery_receipt` is a signed, unprivileged call reachable by any relayer, after verifying the message-proof against the light client, then unconditionally calling `Self::process_delivery_receipt`: [3](#0-2) 

Because Merkle/receipt proof verification (`T::Verifier::verify`) only proves that a log with this shape was emitted at the claimed transaction/receipt key — it does not and cannot assert that `success == true` is the "correct" value to act on — the pallet is trusting the relayer-supplied receipt content's `success` flag without using it as a gate for reward settlement. Whether Ethereum-side execution truly succeeded or failed, the relayer reward is paid and the order is removed identically.

### Impact Explanation
This falls under "duplicate settlement or payout" / "message queues... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" per the pivots. Since the pallet's own state machine explicitly carries a `success` bit intended to reflect whether inbound dispatch on Ethereum succeeded, but pays and settles the pending order unconditionally, a relayer reward is granted even in cases the receipt indicates the message dispatch failed on the destination side — decoupling reward payout from the payload the design intends to gate on. This is a direct implementation bug that compromises intended reward/payout behavior for the outbound bridge delivery flow.

### Likelihood Explanation
Any unprivileged, signed account can call `submit_delivery_receipt` with a proof for a legitimately emitted `InboundMessageDispatched(success=false, ...)` event (e.g., a message that reverted/failed on Ethereum but the gateway event still fired with `success=false`), and the relayer still gets paid and the order is cleared exactly as if it had succeeded — no elevated privileges, malicious peer, or governance/admin misbehavior is required, only a normal relayer submitting a truthful, verifiable receipt for a failed dispatch.

### Recommendation
Gate the reward payment (and/or emit a distinct failure-path event/state) on `receipt.success`, mirroring the audit's recommendation to check the boolean return value explicitly, e.g.:
```rust
ensure!(receipt.success, Error::<T>::DeliveryFailed);
```
or design an explicit, intentional handling path for `success == false` (e.g., no reward, or a different reward tier / retry marker) so `process_delivery_receipt` does not treat successful and failed inbound dispatches identically.

### Proof of Concept
1. A message is queued via `send_message_impl` and stored in `PendingOrders` with a nonzero `fee`.
2. On Ethereum, the corresponding inbound dispatch fails, but the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` (this event is emitted by the gateway regardless of dispatch outcome, as the boolean itself is meant to convey that outcome).
3. A relayer builds a valid proof for this log (real event, real transaction, correct receipt key) and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (proof is valid — it only proves the log exists, not that `success` must be true).
5. `DeliveryReceipt::try_from` decodes `success: false` successfully.
6. `process_delivery_receipt` proceeds: `order.fee > 0` → `T::RewardPayment::register_reward` is called, `PendingOrders` entry removed, `MessageDelivered` event emitted — identical to the success path, despite the message dispatch having failed on Ethereum.

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
