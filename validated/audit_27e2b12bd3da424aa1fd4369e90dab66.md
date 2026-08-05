This confirms the claim. The `DeliveryReceipt` struct at `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` L16-27 explicitly decodes a `success: bool` field from the Ethereum `InboundMessageDispatched` event (`event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)`), and `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` L445-480 never reads `receipt.success` anywhere — it only checks `receipt.gateway` and the existence of `PendingOrders::get(nonce)` before unconditionally calling `T::RewardPayment::register_reward` and removing the order. [1](#0-0) [2](#0-1) 

Audit Report

## Title
Delivery receipt `success` flag is ignored, allowing reward payout and order settlement on failed Ethereum execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` that carries a `success: bool` field sourced directly from the on-chain Ethereum event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)`, but never inspects `receipt.success` before crediting the relayer reward and removing the `PendingOrder`. A relayer can therefore be paid the full committed fee and have the order settled even when the corresponding message execution on Ethereum failed.

## Finding Description
`submit_delivery_receipt` verifies the event proof via `T::Verifier::verify`, decodes the log into a `DeliveryReceipt` via `DeliveryReceipt::try_from`, and forwards it unmodified to `process_delivery_receipt`. [3](#0-2) 

The decode implementation for `DeliveryReceipt` explicitly extracts `success: event.success` from the underlying `InboundMessageDispatched` Solidity event, confirming that this field represents the real execution outcome on Ethereum, distinct from mere event occurrence. [4](#0-3) 

`process_delivery_receipt` only checks `T::GatewayAddress::get() == receipt.gateway` and that a `PendingOrder` exists for `receipt.nonce`. It never reads `receipt.success` anywhere in the function body — it unconditionally calls `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` when `order.fee > 0`, then removes the order from `PendingOrders` and emits `Event::MessageDelivered`. [2](#0-1) 

Existing guards — `T::Verifier::verify` (proves the event log genuinely originated from the configured gateway/chain), the gateway-address equality check, and the pending-order existence check — only establish authenticity and route/nonce binding of the event. None of them validate that the command execution itself succeeded on the Ethereum side, which is precisely what the `success` field is meant to convey.

## Impact Explanation
This breaks the required invariant that message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically. A relayer can obtain the full `order.fee` reward and cause `PendingOrders` to be settled for messages whose execution on Ethereum reverted, as long as the Gateway contract still emits a valid `InboundMessageDispatched` event with `success = false`. This is a duplicate/unbacked-style payout: BridgeHub's reward accounting is settled disconnected from genuine successful delivery.

## Likelihood Explanation
The path is reachable by any signed account through the public `submit_delivery_receipt` extrinsic, requiring only a valid Ethereum event proof for a real gateway-emitted `InboundMessageDispatched` event — including one where `success = false` (e.g., because the encoded command reverted on Ethereum but the Gateway logs the outcome regardless). No malicious peer, validator, collator, governance, or leaked key is needed; a standard relayer role suffices.

## Recommendation
Validate `receipt.success` in `process_delivery_receipt` before crediting the reward and/or removing the `PendingOrder`. For `success == false`, take a distinct path (e.g., emit a `MessageDeliveryFailed` event and either withhold reward, reduce it, or handle failed-order accounting separately) instead of unconditionally paying `order.fee` and clearing the order.

## Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrder { nonce, fee, .. }`.
2. The corresponding command is delivered to Ethereum and its execution reverts, but the Gateway contract still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains the event log/proof and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (event genuinely occurred); `DeliveryReceipt::try_from` decodes `success = false` along with `nonce`, `reward_address`, `gateway`.
5. `process_delivery_receipt` checks only `gateway` equality and `PendingOrders::get(nonce)` existence, then unconditionally calls `T::RewardPayment::register_reward(&reward_account, .., order.fee)` and removes the order — as demonstrated by the passing tests in `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs` (e.g. lines 104–121), which construct receipts with `success: true` and observe reward registration; setting `success: false` in the same test would produce identical accepted behavior, confirming the flag has no effect on the outcome.

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
