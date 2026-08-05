Audit Report

## Title
`process_delivery_receipt` in Snowbridge outbound-queue-v2 ignores the on-chain `success` flag, rewarding relayers and clearing pending orders even when the Ethereum-side execution failed - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt` decodes a `success: bool` field directly from the Ethereum `InboundMessageDispatched` event log, but `Pallet::process_delivery_receipt` never reads or branches on this field before paying the relayer reward and removing the `PendingOrder`. This is confirmed by direct inspection of the pallet: [1](#0-0)  and the field's origin in the primitives crate [2](#0-1) .

## Finding Description
`submit_delivery_receipt` verifies the event log/proof via `T::Verifier::verify`, decodes it into a `DeliveryReceipt` via `TryFrom<&Log>`, and forwards it directly to `process_delivery_receipt` without any success check: [3](#0-2) . The `TryFrom` implementation faithfully decodes `success` from the on-chain event, meaning `success: false` is just as provable via proof as `success: true`: [4](#0-3) .

`process_delivery_receipt` itself only validates `receipt.gateway`, resolves the reward account, looks up `PendingOrders` by `receipt.nonce`, unconditionally pays `order.fee` to the reward account, removes the pending order, and emits `MessageDelivered` — `receipt.success` is never read: [5](#0-4) . A grep across `bridges/snowbridge/pallets/outbound-queue-v2/**` for `success` returns zero matches, confirming the field is decoded but never consulted in this pallet's production logic.

The pallet's own doc comment describes the receipt as finalizing state "when the message has been verified and executed" — the code only verifies the proof of the log, not that the underlying dispatch succeeded: [6](#0-5) . Prior to this receipt being processed, `do_process_message` on Asset Hub/BridgeHub side has already committed the message and created the `PendingOrder` with the fee, corresponding to assets already withdrawn/reserved cross-chain: [7](#0-6) .

## Impact Explanation
This satisfies the "duplicate settlement or payout" / "permanent bridge-state lock" impact category: the relayer reward (`order.fee`) is paid and the `PendingOrder` for `nonce` is deleted identically whether the Ethereum-side dispatch succeeded or reverted. Since no other code path in this pallet inspects `success` to trigger a refund or reroute of the previously committed source-side assets, a failed dispatch is indistinguishable from a successful one from the bridge's bookkeeping perspective, and the corresponding `PendingOrders` entry — the only on-chain record tying the nonce to its fee/block — is irrecoverably destroyed.

## Likelihood Explanation
This is reachable via the unprivileged, ordinary `submit_delivery_receipt` extrinsic by any relayer holding a valid proof of the Ethereum event log. No malicious relayer/validator behavior is required for the vulnerability to manifest — any real-world Ethereum-side execution failure (revert, insufficient gas, dispatch failure) legitimately produces a `success: false` event, and the current code deterministically treats it as a successful, fee-eligible delivery.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`. On `true`, keep current behavior. On `false`, avoid treating it as a completed/rewarded delivery — emit a distinct failure event (e.g., `MessageDispatchFailed`) and drive appropriate handling (refund or alternate resolution) for the corresponding `PendingOrder` before removing it, rather than unconditionally paying the reward and deleting the order.

## Proof of Concept
1. On Asset Hub, submit an XCM with `WithdrawAsset` + `InitiateTransfer`/`ReserveWithdraw` targeting Ethereum, generating an outbound message with nonce `n` and inserting `PendingOrder { nonce: n, fee, .. }` via `do_process_message`.
2. Assume the Ethereum Gateway contract execution for nonce `n` reverts, emitting `InboundMessageDispatched(n, topic, success=false, reward_address)`.
3. A relayer calls `submit_delivery_receipt` with a valid proof of that log; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly.
4. `process_delivery_receipt` ignores `receipt.success`, pays `order.fee` to the reward account, and calls `<PendingOrders<T>>::remove(n)`, emitting `MessageDelivered`.
5. The relayer is rewarded as if delivery succeeded, and the bookkeeping for nonce `n` is destroyed with no refund path for the assets already withdrawn on Asset Hub in step 1.

### Citations

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
