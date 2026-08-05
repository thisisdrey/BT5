Audit Report

## Title
Outbound queue v2 pays relayer reward and closes the pending order without checking the delivery `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt`, decoded from the Ethereum `InboundMessageDispatched` event, carries a `success: bool` field indicating whether the relayed message dispatch succeeded or failed on Ethereum. [1](#0-0) 
`Pallet::process_delivery_receipt` decodes this field into `receipt.success` but never reads or branches on it — it unconditionally pays the relayer reward and unconditionally removes the `PendingOrder` regardless of whether the underlying delivery actually succeeded. [2](#0-1) 

## Finding Description
The pallet's documented flow states that once "the message has been verified and executed, the relayer will call `submit_delivery_receipt`" to fetch the pending order, pay the reward, and remove the order. [3](#0-2) 

`submit_delivery_receipt` is a permissionless, signed extrinsic that only requires a valid Ethereum inclusion proof verified by `T::Verifier::verify`; any signed account can call it once such a proof exists: [4](#0-3) 

`DeliveryReceipt::try_from` decodes the `success` field from the Solidity `InboundMessageDispatched` event log: [5](#0-4) 

But in `process_delivery_receipt`, after fetching `order` by `nonce`, the code pays the reward via `T::RewardPayment::register_reward` whenever `order.fee > 0`, then unconditionally removes `PendingOrders[nonce]` and emits `Event::MessageDelivered` — with no reference to `receipt.success` anywhere in this function: [6](#0-5) 

This means a genuine Ethereum event reporting `success = false` (e.g., the inbound command execution reverted on the destination) is processed identically to `success = true`: the relayer is rewarded and the order is torn down with no remaining record for reconciliation, retry, or re-fee.

## Impact Explanation
This violates the invariant that bridge receipts and payout state must only advance to final settlement after the delivery has actually succeeded. The reward (an on-chain payout, per `T::RewardPayment::register_reward`) is disbursed unconditionally, and the sole persistent record of the pending message (`PendingOrders[nonce]`) is deleted regardless of the Ethereum-reported outcome. This is an underpriced/incorrect-settlement path: a public, unprivileged extrinsic can force payout and definitive closure of an order tied to a failed delivery, with no governance or retry mechanism analogous to `pallet-message-queue`'s `Overweight`/execute_overweight handling.

## Likelihood Explanation
Any signed account can invoke `submit_delivery_receipt` as soon as a genuine Ethereum inclusion proof exists for an `InboundMessageDispatched` log with `success = false` — a normal occurrence such as insufficient destination gas or an agent execution revert, requiring no privileged role, governance action, or compromised relayer/prover assumption. This is reachable through ordinary usage of the public extrinsic.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: on `false`, skip or adjust the reward payment and transition the `PendingOrder` into a distinct failure/retry state (or emit a `MessageDeliveryFailed` event) instead of unconditionally paying and removing the order as if delivery succeeded.

## Proof of Concept
1. A message is queued, producing `PendingOrder { nonce, fee, .. }` via `do_process_message` [7](#0-6) .
2. On Ethereum, the Gateway dispatches the inbound message, but the command execution fails, so `InboundMessageDispatched` is emitted with `success = false`.
3. Any signed account calls `submit_delivery_receipt` with a valid proof for this event [4](#0-3) .
4. `process_delivery_receipt` pays `order.fee` to the reward account and removes `PendingOrders[nonce]`, emitting `MessageDelivered`, exactly as if `success` had been `true` [6](#0-5) .

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-51)
```rust
impl TryFrom<&Log> for DeliveryReceipt {
	type Error = DeliveryReceiptDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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
