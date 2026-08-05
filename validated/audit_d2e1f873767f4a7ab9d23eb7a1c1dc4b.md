The code confirms the claim precisely as described. `DeliveryReceipt` decodes `success: bool` from the on-chain `InboundMessageDispatched` event [1](#0-0) , but `process_delivery_receipt` only uses `receipt.gateway`, `receipt.nonce`, and `receipt.reward_address` — `receipt.success` is never read anywhere in the function body before paying the reward and removing the order [2](#0-1) . The order was created unconditionally at message-acceptance time regardless of eventual delivery outcome [3](#0-2) , and the doc-comment for the flow confirms the intended design only mentions verify → fetch order → pay reward → remove order, without any success gating [4](#0-3) .

Audit Report

## Title
`process_delivery_receipt` settles relayer reward and removes the pending order without checking the on-chain `success` flag of the delivery - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`DeliveryReceipt`, decoded from the verified `InboundMessageDispatched` Ethereum event, carries an explicit `success: bool` field indicating whether the Gateway contract's execution of the message actually succeeded. `Pallet::process_delivery_receipt` never inspects this field: it pays the relayer's `order.fee` and permanently removes the `PendingOrder` for the nonce regardless of whether `success` is `true` or `false`, so failed on-chain deliveries are settled identically to successful ones.

## Finding Description
`DeliveryReceipt::try_from` decodes `success` straight from the verified log's `InboundMessageDispatched` event [5](#0-4) . In `process_delivery_receipt`, only `receipt.gateway` is checked (against the configured `GatewayAddress`) and `receipt.reward_address`/`receipt.nonce` are used to route the reward and locate the `PendingOrder`; `receipt.success` is bound in the struct but never read [6](#0-5) . Once `<PendingOrders<T>>::remove(nonce)` runs, there is no other on-chain record of the message's delivery state, no retry path, and no refund path — `MessageDelivered` is emitted unconditionally. The pallet's own doc-comment for the extrinsic flow ("verify the message ... fetch the pending order ... pay reward ... remove the order") corroborates that no success gating was designed into this path [4](#0-3) .

## Impact Explanation
The `PendingOrder` and its associated fee are created unconditionally when a message is first queued on the BridgeHub side, independent of downstream Ethereum execution success [3](#0-2) . If the Gateway contract's execution of the corresponding commands fails on Ethereum (`success == false`) — e.g., an asset-unlock or transact command reverts — the relayer is nonetheless fully paid `order.fee` and the order is permanently deleted, with no mechanism to retry delivery or unlock/refund the assets tied to that nonce. This matches the impact gate's "payout state advances without confirmed successful settlement" / duplicate or mis-settled bridge reward payout, and can leave the originating assets permanently stranded since the nonce's tracking state is destroyed.

## Likelihood Explanation
Any unprivileged relayer can trigger this by submitting a legitimately-provable `EventProof` for a real `InboundMessageDispatched` log with `success = false` (which occurs naturally on out-of-gas or reverted downstream commands on the Gateway contract) via the public `submit_delivery_receipt` extrinsic. No malicious peer, validator, governance action, or compromised key is required — only a normal failure occurring on the Ethereum side plus honest relaying of the resulting proof.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only pay the relayer reward and remove the `PendingOrder` on `true`; on `false`, avoid full reward settlement of the failed portion and either retain the order in a distinct failed/retryable state or emit a dedicated failure event and trigger a refund/unlock path for the associated commands' assets, instead of unconditionally treating the nonce as delivered.

## Proof of Concept
1. A message is queued via `do_process_message`, inserting `PendingOrders[nonce] = { fee, block_number }` (see lines 426-443).
2. The Gateway contract on Ethereum attempts execution but a downstream command reverts, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid `EventProof` for this real log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success=false` correctly, but `process_delivery_receipt` never reads `receipt.success`; it pays `order.fee` and removes `PendingOrders[nonce]`, emitting `MessageDelivered`.
5. The existing test suite only constructs receipts with implicit/`success: true` semantics (e.g. `submit_delivery_receipt_succeeds_after_unhalt`) and never exercises the `false` branch, confirming no code path differentiates on this field.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L36-41)
```rust
//! 10. When the message has been verified and executed, the relayer will call the extrinsic
//!     `submit_delivery_receipt` to:
//! 	a. Verify the message with proof for a transaction receipt containing the event log,
//! 	   same as the inbound queue verification flow
//! 	b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order
//!    	c. Remove the order from `PendingOrders` map storage by nonce
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-443)
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

			Ok(true)
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
