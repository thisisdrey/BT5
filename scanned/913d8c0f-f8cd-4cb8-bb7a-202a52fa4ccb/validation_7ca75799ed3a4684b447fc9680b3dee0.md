Confirmed: `success` is never referenced anywhere in the outbound-queue-v2 pallet — the field is decoded from the Ethereum event log but completely discarded before the reward payment path is taken. [1](#0-0) [2](#0-1) 

### Title
Relayer reward is paid unconditionally regardless of `DeliveryReceipt.success`, ignoring the on-chain dispatch outcome - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` decodes the Ethereum `InboundMessageDispatched` event into a `DeliveryReceipt` struct that carries a `success: bool` field reporting whether the message was actually dispatched successfully on the Ethereum Gateway contract, but this field is never checked. The pallet unconditionally pays the relayer reward and removes the `PendingOrder` as soon as a validly-proven receipt for a known nonce is submitted, whether or not the underlying message execution on Ethereum actually succeeded.

### Finding Description
`DeliveryReceipt` is decoded from the `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` Solidity event log: [1](#0-0) 

`submit_delivery_receipt` verifies the beacon/transaction-receipt proof, decodes the event into `DeliveryReceipt`, and forwards it to `process_delivery_receipt`: [3](#0-2) 

`process_delivery_receipt` validates the gateway address, resolves the reward account, looks up the `PendingOrder` by nonce, pays `order.fee` via `T::RewardPayment::register_reward`, and removes the order — at no point does it read or branch on `receipt.success`: [2](#0-1) 

A grep across the whole pallet confirms `success` is never referenced anywhere in `outbound-queue-v2`, i.e. the field is dead code from the reward-accounting perspective. This mirrors exactly the reported bug class ("ignores return value... if the return value is the success status, then it's important to check it"): here the boolean success indicator coming back from the destination-side execution is decoded but discarded before advancing payout state.

### Impact Explanation
Per the required pivots, "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." This invariant is broken: `PendingOrders` removal (marking the message "settled") and the reward payment both proceed even when `success == false`, i.e., when the Gateway contract's dispatch of the command actually failed on Ethereum (e.g., insufficient gas supplied by the relayer, or a reverting command). This decouples the relayer's reward from the correctness/success of the work they were supposed to perform, allowing rewards for incomplete or failed bridge delivery — a form of unbacked payout from the reward/treasury-backed pool for underpriced or failed work, and it terminally clears the `PendingOrder` so there is no separate retry/penalty path once the (unconditional) reward has been paid.

### Likelihood Explanation
The path is public and unprivileged: any signed account can call `submit_delivery_receipt` for any nonce, as long as they supply a valid proof of a real `InboundMessageDispatched` event log for that nonce/gateway. The event itself is genuine (not forged), so an honest relayer could trivially trigger `success == false` (e.g., by intentionally or accidentally submitting the message with insufficient gas on the Ethereum side) and still collect full payment on BridgeHub, since the extrinsic performs no gas-sufficiency or success check before rewarding and clearing the order.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying the reward. On `success == false`, either withhold/reduce the reward, re-queue the order for retry, or route to a distinct failure-handling/event path (e.g., emit a `MessageDeliveryFailed` event) instead of treating it identically to a successful delivery.

### Proof of Concept
1. A message with nonce `N` and non-zero `fee` is queued and committed, creating `PendingOrders::<T>::get(N)`.
2. An account (relayer) submits the message to the Ethereum Gateway contract but deliberately supplies gas just low enough that the Gateway's internal command dispatch fails, causing the Gateway to emit `InboundMessageDispatched(N, topic, success=false, reward_address)` (a legitimate on-chain event, not forged).
3. The relayer collects the transaction receipt/proof for this event and calls `submit_delivery_receipt` on BridgeHub.
4. `T::Verifier::verify` succeeds (real proof), `DeliveryReceipt::try_from` decodes `success = false` correctly.
5. `process_delivery_receipt` at `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:445-480` never inspects `receipt.success`, so it pays `order.fee` via `T::RewardPayment::register_reward` and removes `PendingOrders::<T>` for nonce `N`, emitting `MessageDelivered`.
6. Result: the relayer is paid in full for a delivery that failed on the destination chain, and the pending order is permanently cleared with no re-delivery/penalty mechanism.

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
