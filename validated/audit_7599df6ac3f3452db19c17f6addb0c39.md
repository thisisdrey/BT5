This confirms the claim exactly as described. The code review shows:

1. `DeliveryReceipt` struct has a `success: bool` field decoded from the on-chain Ethereum event `InboundMessageDispatched` [1](#0-0) , correctly populated from the ABI-decoded log in `TryFrom<&Log>` [2](#0-1) .

2. `process_delivery_receipt` only checks the gateway address, resolves the reward account, fetches the `PendingOrder`, and unconditionally pays the reward (if `fee > 0`) and removes the order — `receipt.success` is never read or branched on anywhere in this function [3](#0-2) .

3. The `PendingOrder` is created with a nonzero `fee` when a message is queued [4](#0-3) , and this is the state that gets paid out/cleared regardless of delivery outcome.

This is a genuine, unprivileged, reachable issue: `submit_delivery_receipt` is a public signed extrinsic requiring only a valid `Verifier::verify` proof — no special origin — and Ethereum-side execution failures (reverts) are a normal occurrence, not requiring malicious behavior. The bug results in an unconditional payout and permanent removal of tracking state for a nonce whose corresponding message did not actually succeed on Ethereum, matching the "duplicate settlement or payout" / "runtime bugs that compromise intended behavior" impact category.

Audit Report

## Title
Relayer reward is paid and `PendingOrder` cleared regardless of `DeliveryReceipt.success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` verifies the Ethereum event proof and decodes it into a `DeliveryReceipt` containing a `success: bool` field indicating whether the corresponding message actually executed successfully on the Ethereum Gateway contract, but the payout logic never reads or checks this field before paying the relayer reward and removing the `PendingOrder`.

## Finding Description
`process_delivery_receipt` verifies only `T::GatewayAddress::get() == receipt.gateway`, resolves the `reward_account`, fetches the `PendingOrder` by `receipt.nonce`, and if `order.fee > 0` unconditionally calls `T::RewardPayment::register_reward`, then unconditionally calls `<PendingOrders<T>>::remove(nonce)` [3](#0-2) . The `receipt.success` field, populated directly from the Ethereum `InboundMessageDispatched` event's `success` parameter [5](#0-4) , is never inspected in this control flow. This means a delivery event that failed on Ethereum (`success = false`) is treated identically to a successful one.

## Impact Explanation
A relayer submitting a legitimate, validly-proved event log for a Gateway-emitted delivery event with `success = false` still receives the full relayer fee via `T::RewardPayment::register_reward` and causes `PendingOrders::remove(nonce)` to permanently clear the order tracking state [6](#0-5) . This is an unbacked payout — reward for a delivery that did not complete — and permanently retires order-tracking state for a failed delivery, matching the "duplicate settlement or payout" / "runtime bugs that compromise intended behavior" impact category.

## Likelihood Explanation
`submit_delivery_receipt` is a public, unprivileged, signed extrinsic requiring only `T::Verifier::verify` to succeed on an authentic Ethereum log [7](#0-6) . Ethereum-side command execution failures (reverts, out-of-gas) are a normal, non-adversarial occurrence, so any relayer relaying a genuinely failed delivery receipt collects payment without needing to act maliciously, making this readily reachable in ordinary operation.

## Recommendation
Check `receipt.success` in `process_delivery_receipt` before paying the reward and removing the order: only pay the relayer and clear the `PendingOrder` when `receipt.success` is `true`; on `false`, retain the order (for retry/refund logic) or move it to a distinct failed state, and avoid crediting `T::RewardPayment`.

## Proof of Concept
1. A message is queued via `do_process_message`, inserting `PendingOrders::<T>::insert(nonce, PendingOrder { nonce, fee, block_number })` with `fee > 0` [4](#0-3) .
2. The corresponding Ethereum command execution reverts, and the Gateway emits `InboundMessageDispatched` with `success = false`.
3. Any signed relayer submits this log with a valid proof via `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` proceeds identically to a successful case: gateway matches, order is found, `order.fee > 0` triggers `T::RewardPayment::register_reward`, and `PendingOrders::remove(nonce)` deletes the order — despite the delivery having failed on Ethereum.

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L298-316)
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
