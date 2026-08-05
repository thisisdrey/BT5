This confirms the claim exactly as described. The code at `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 446-480 in `process_delivery_receipt` never inspects `receipt.success` before paying `T::RewardPayment::register_reward` and removing the `PendingOrders` entry. The `DeliveryReceipt` struct at `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` lines 14-27 decodes `success` from the Ethereum event but it's discarded/unused in the pallet logic.

Audit Report

## Title
Relayer reward paid on `submit_delivery_receipt` regardless of `DeliveryReceipt.success` value - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` pays the relayer reward via `T::RewardPayment::register_reward` and unconditionally removes the `PendingOrders` entry for a nonce without ever branching on the `success` field decoded from the Ethereum `InboundMessageDispatched` event. A delivery receipt reporting execution failure on Ethereum (`success == false`) is treated identically to a successful delivery, resulting in reward payout and permanent closure of the order without confirmed successful execution.

## Finding Description
`submit_delivery_receipt` decodes an `EventProof`, verifies it via `T::Verifier::verify`, and decodes it into a `DeliveryReceipt` struct that carries a `success: bool` field sourced from the Ethereum event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` [1](#0-0) . The extrinsic then calls `Self::process_delivery_receipt(relayer, receipt)` [2](#0-1) .

`process_delivery_receipt` only checks the `gateway` address and that `PendingOrders::get(nonce)` exists; it never reads `receipt.success` before unconditionally paying `order.fee` via `T::RewardPayment::register_reward` and removing the `PendingOrders` entry [3](#0-2) . The `success` field is decoded in `DeliveryReceipt::try_from` at [4](#0-3)  but is dead data as far as `process_delivery_receipt` is concerned — it is never referenced in the function body. Existing guards (`GatewayAddress` check and `PendingOrders` existence check) validate authenticity/routing but do not validate outcome, so they are insufficient to prevent settlement on a failed dispatch.

## Impact Explanation
This allows the relayer reward (`order.fee`) to be paid, and the `PendingOrders` record for that nonce to be permanently removed, even when the Ethereum-side dispatch failed (`success == false`). This is a payout/settlement-state correctness bug matching the "duplicate settlement or payout" / "reward payout without atomic success" impact class: reward and pending-order state advance without confirmed successful execution, and there is no visible retry path for a nonce whose order has already been removed despite failed execution.

## Likelihood Explanation
The path is reachable by any signed account (`ensure_signed(origin)` is the only origin check) submitting a genuine, verifiable Ethereum log/proof for a real `InboundMessageDispatched` event with `success = false` [5](#0-4) . No privileged origin, governance, or compromised relayer/prover assumption is required — this is exactly the intended flow when message execution genuinely reverts on Ethereum (e.g., gas exhaustion), which is precisely why the `success` field exists in the event schema.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before paying the reward and/or removing the `PendingOrders` entry: on `success == false`, withhold or reduce the reward, retain the order for a future correct/retry receipt, and emit a distinct event (e.g., `MessageDeliveryFailed`) instead of unconditionally emitting `MessageDelivered` and settling the reward.

## Proof of Concept
1. A message is enqueued via `do_process_message`, inserting a `PendingOrder { nonce, fee, block_number }` into `PendingOrders` [6](#0-5) .
2. Execution of that message on the Ethereum Gateway reverts/fails; the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer captures this log with a valid proof and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the log is real and included), and `DeliveryReceipt::try_from` decodes `success = false` without error.
5. `process_delivery_receipt` passes the `gateway` check and finds the matching `PendingOrders` entry, then unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and removes the pending order — paying the relayer as though the message succeeded, despite `receipt.success == false`.

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
