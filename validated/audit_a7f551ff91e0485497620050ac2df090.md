This confirms the claim exactly matches the current code.

`DeliveryReceipt` includes a `success: bool` field decoded straight from the Ethereum `InboundMessageDispatched` event log [1](#0-0) , and the decode path pulls `success` from the ABI-decoded event unconditionally [2](#0-1) . `process_delivery_receipt` only checks the gateway address and pending nonce, then pays the reward and removes the order without ever reading `receipt.success` [3](#0-2) . The public extrinsic `submit_delivery_receipt` is reachable by any signed relayer and only verifies inclusion (not success semantics) before calling into this function [4](#0-3) .

Audit Report

## Title
Relayer reward is paid and `PendingOrder` is settled without checking `DeliveryReceipt::success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event log includes a `success` boolean field indicating whether the corresponding message actually executed successfully on the Ethereum Gateway contract. `Pallet::process_delivery_receipt` decodes and stores this field on the struct but never reads it when deciding to pay the relayer reward and remove the `PendingOrder`.

## Finding Description
The receipt type explicitly carries delivery status via the `success` field, decoded from the ABI-encoded `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` event [5](#0-4) . However, `process_delivery_receipt` only validates the gateway address (`receipt.gateway`) and the pending nonce (`receipt.nonce`); it pays `order.fee` to `reward_account` and removes the `PendingOrders` entry unconditionally, without ever branching on `receipt.success` [6](#0-5) .

`T::Verifier::verify` (invoked in `submit_delivery_receipt`) only checks that the log/proof are cryptographically included in a finalized Ethereum block — it proves the event genuinely happened, not that the dispatched command succeeded [7](#0-6) . A legitimately-emitted event with `success=false` (which the Gateway contract emits when the dispatched command reverts/fails) is accepted and processed identically to a `success=true` event, since nothing downstream inspects the field. This violates the invariant that receipts and payout state must only advance after execution actually succeeds.

## Impact Explanation
A relayer is rewarded for "delivering" a message that failed to execute on Ethereum, while the `PendingOrder` is deleted as if properly settled — an incorrect/duplicate-class settlement where the fee is paid for work that produced no successful outcome, with no re-queue path for the now-lost order. This maps to the "duplicate settlement or payout" and "public underpriced work" impact categories in the Polkadot SDK impact gate.

## Likelihood Explanation
Reachable by any unprivileged, signed relayer account via the public `submit_delivery_receipt` extrinsic, using a genuinely emitted and genuinely provable `success=false` log — an expected occurrence in normal bridge operation (e.g., out-of-gas command execution), not requiring any attacker-crafted proof or privileged access.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only pay the reward when `success == true`; on `success == false`, either re-queue/retry the message or remove the order without paying the reward, emitting a distinct failure event so on-chain state accurately reflects the real Ethereum-side outcome.

## Proof of Concept
1. `do_process_message` enqueues a message, inserting `PendingOrders[nonce] = { fee > 0, block_number }` [8](#0-7) .
2. On Ethereum, the corresponding command execution fails/reverts; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits `submit_delivery_receipt` with a valid inclusion proof of that log; `T::Verifier::verify` succeeds (proves inclusion only) and `DeliveryReceipt::try_from` decodes `success: false` without error [2](#0-1) .
4. `process_delivery_receipt` ignores `receipt.success`, pays `order.fee` to `reward_account`, and removes the order — identical to the success path [9](#0-8) .

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L38-51)
```rust
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
