Audit Report

## Title
Relayer reward and order settlement ignore the on-chain `success` flag in `DeliveryReceipt` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt::try_from` decodes the `success: bool` field directly from the Ethereum `InboundMessageDispatched` event log, but `process_delivery_receipt` never reads or checks `receipt.success` before paying the relayer reward and permanently removing the corresponding `PendingOrder`. As a result, a relayer can submit a valid proof for a failed/reverted Ethereum-side dispatch and still receive the full reward while the order is irrecoverably deleted.

## Finding Description
In `submit_delivery_receipt`, the extrinsic verifies the Merkle/receipt proof via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt` (which includes `success`), and passes it to `Self::process_delivery_receipt` [1](#0-0) . The `DeliveryReceipt` struct explicitly carries the `success` field decoded from the `InboundMessageDispatched` Solidity event [2](#0-1) .

Inside `process_delivery_receipt`, the only validation performed is that `receipt.gateway` matches `T::GatewayAddress`; it then unconditionally looks up `PendingOrders[nonce]`, pays `order.fee` to the reward account via `T::RewardPayment::register_reward` whenever `order.fee > 0`, and removes the order from storage — all without ever inspecting `receipt.success` [3](#0-2) . The pending order was originally created with a nonzero `fee` when the message was queued [4](#0-3) .

This violates the invariant that payout/settlement state must only advance after the underlying delivery genuinely succeeds — the `success` field exists specifically to distinguish a successful dispatch from a failed one, and the code discards it.

## Impact Explanation
This is a duplicate/incorrect-settlement and unbacked-payout issue against bridge reward funds: any signed account acting as a relayer can be rewarded for deliveries that failed on Ethereum, and the `PendingOrder` is deleted regardless of actual outcome, eliminating any retry/recovery path and leaving no on-chain record that redelivery is needed. This falls squarely within the "theft or unbacked mint or unlock" / "duplicate settlement or payout" impact category, since payout state advances despite the on-chain execution outcome (`success = false`) that should have gated it.

## Likelihood Explanation
High. Exploitation requires only a normal, permissionless call to the public extrinsic `submit_delivery_receipt` with a legitimate proof of any real Ethereum transaction that emits `InboundMessageDispatched` with `success = false` and a matching `nonce`/`gateway`. No validator, governance, or privileged access is needed — `T::Verifier::verify` only proves the log's existence in a finalized block, not the semantic correctness of `success`, and `process_delivery_receipt` performs no check on it.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before paying the reward or removing the order. When `success == false`, either reject/no-op without consuming `PendingOrders[nonce]` (permitting retry/redelivery) or implement an explicit failure-settlement path (no reward, requeue, or refund), rather than treating `success = false` identically to a genuine successful delivery.

## Proof of Concept
1. A message is queued via `do_process_message`, inserting `PendingOrders[nonce] = { fee > 0, .. }` [5](#0-4) .
2. The corresponding Ethereum-side dispatch reverts/fails, and the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer builds the standard receipt/execution proof for that transaction and calls `submit_delivery_receipt(origin, event)` [6](#0-5) .
4. `T::Verifier::verify` succeeds since the log genuinely exists; `DeliveryReceipt::try_from` decodes `success = false`.
5. `process_delivery_receipt` ignores `success`, pays `order.fee` to `reward_account`, and removes `PendingOrders[nonce]` [7](#0-6)  — identical behavior to a genuinely successful delivery, confirming the missing check.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-51)
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

#[derive(Copy, Clone, Encode, Decode, Eq, PartialEq, Debug, TypeInfo)]
pub enum DeliveryReceiptDecodeError {
	DecodeLogFailed,
	DecodeAccountFailed,
}

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
