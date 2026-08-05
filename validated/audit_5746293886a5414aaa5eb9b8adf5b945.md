Confirms `success` is only referenced at the single assignment site in `delivery_receipt.rs` and never read anywhere else in the codebase, matching the claim exactly.

Audit Report

## Title
`process_delivery_receipt` ignores the `success` field of the delivery receipt and always pays the relayer reward - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt` decodes a `success: bool` field from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event, indicating whether the message actually executed successfully on the Ethereum Gateway. `Pallet::process_delivery_receipt` validates gateway and nonce but never inspects `receipt.success`, unconditionally paying the reward and clearing the pending order regardless of the actual dispatch outcome.

## Finding Description
`DeliveryReceipt::try_from` decodes the `success` flag straight from the verified log: [1](#0-0) . The struct definition explicitly carries this field as `Delivery status`: [2](#0-1) .

`submit_delivery_receipt` is a public, permissionless extrinsic that only checks proof validity via `T::Verifier::verify` and decodes the receipt, then forwards directly to `process_delivery_receipt`: [3](#0-2) .

`process_delivery_receipt` checks `receipt.gateway` and looks up the pending order by `receipt.nonce`, but never reads `receipt.success` before paying `order.fee` to `reward_account` and removing the order from `PendingOrders`: [4](#0-3) . A repo-wide search confirms `success` is assigned only at the decode site and is never read anywhere else in `outbound-queue-v2` or the primitives crate. Neither `T::Verifier::verify` nor `DeliveryReceipt::try_from` validate that `success == true` — they only prove the log was genuinely emitted for the matching gateway/nonce, not that the Ethereum-side dispatch succeeded.

## Impact Explanation
This matches the "duplicate/incorrect settlement despite ignored status code" impact class from the required impact gate. When a real cross-chain command reverts on the Ethereum Gateway, the Gateway still legitimately emits `InboundMessageDispatched` with `success: false`. A relayer who submits this legitimate, verified receipt causes the pallet to pay `order.fee` to the reward account and remove `PendingOrders[nonce]` — settling and finalizing bridge delivery bookkeeping and disbursing fees for a message that did not actually execute correctly on the destination side. This is a public underpriced/incorrect settlement affecting bridge processing state, since payout state advances without confirming the intended (successful) execution outcome encoded in the same already-verified receipt.

## Likelihood Explanation
The path requires no privileged actor: any signed account can call `submit_delivery_receipt` with a legitimately produced Ethereum receipt for a message whose dispatch failed on-chain — this occurs naturally whenever a real cross-chain command reverts (e.g., out-of-gas, invalid command, destination-side revert), not only via malicious crafting. The verifier and decode checks gating this function do not examine `success`, so the flawed downstream logic in `process_delivery_receipt` is reached on every dispatch-failure receipt with no extra attacker effort, and is fully repeatable for every failed dispatch.

## Recommendation
Check `receipt.success` in `process_delivery_receipt` and branch explicitly: only pay the full relayer reward and settle as `MessageDelivered` when `success == true`. For `success == false`, take a distinct code path — e.g., still clear the `PendingOrder` for accounting purposes but skip/reduce the reward, and emit a distinct event (e.g., `MessageDispatchFailed`) reflecting the true outcome, so payout state is never advanced as if execution succeeded when it did not.

## Proof of Concept
1. A message is queued via `do_process_message`, inserting `PendingOrders[nonce]` with `fee > 0`: [5](#0-4) .
2. On Ethereum, the Gateway's dispatch attempt reverts, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid proof for this legitimately emitted, failed-dispatch log and calls `submit_delivery_receipt`; `T::Verifier::verify` succeeds because it only checks proof validity, not the `success` flag: [6](#0-5) .
4. `DeliveryReceipt::try_from` decodes `success: false` correctly, but `process_delivery_receipt` never reads this field before paying the reward and removing the order: [7](#0-6) .
5. Existing tests (`poc_m1` and similar) only assert `Halted`/gateway/nonce guard behavior with `success: true` receipts, and do not test/assert the `success: false` path, confirming this branch is unguarded in the test suite.

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
