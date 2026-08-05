This confirms the claim: `process_delivery_receipt` reads `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` but never reads `receipt.success` anywhere in its body [1](#0-0) , and `DeliveryReceipt` genuinely carries a `success: bool` field decoded straight from the Ethereum `InboundMessageDispatched` event log [2](#0-1) . Since `success` comes directly from the on-chain Ethereum event and is never gated on in the settlement logic, a receipt proving a genuinely reverted/failed Gateway dispatch (`success: false`) is treated identically to a success: the reward is paid and `PendingOrders::remove(nonce)` executes unconditionally.

Audit Report

## Title
`OutboundQueueV2::process_delivery_receipt` pays relayer reward and permanently removes the pending order without checking `DeliveryReceipt.success` - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`process_delivery_receipt` verifies the receipt's `gateway` field and looks up the `PendingOrder` by `nonce`, but never inspects the `success` field of the decoded `DeliveryReceipt` before calling `T::RewardPayment::register_reward` and unconditionally executing `<PendingOrders<T>>::remove(nonce)`. Since `success` is decoded directly from the real `InboundMessageDispatched` Ethereum event log (true reflection of whether the Gateway's command dispatch reverted), a legitimately-proven receipt for a failed delivery is settled exactly like a successful one, paying the relayer and irrecoverably destroying the pending order/fee state.

## Finding Description
The function body is:
```rust
pub fn process_delivery_receipt(...) -> DispatchResult {
    ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
    let reward_account = ...;
    let nonce = receipt.nonce;
    let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
    if order.fee > 0 {
        T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
    }
    <PendingOrders<T>>::remove(nonce);
    Self::deposit_event(Event::MessageDelivered { nonce });
    Ok(())
}
``` [1](#0-0) 

The `receipt.success` field is populated from the `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` Solidity event via `TryFrom<&Log>` in the primitives crate, so a `false` value is a genuine, verifiable outcome (the Gateway's on-Ethereum dispatch of the command reverted), not something a relayer can fabricate independently of proof verification [3](#0-2) . The pallet's own module-level docs describe the intended flow as paying the reward and removing the order only "when the message has been verified and executed" successfully [4](#0-3) , but nothing in the settlement code branches on `success`. A proof that verifies (i.e., a real event log for that nonce) with `success: false` is settled identically to `success: true`: the reward is registered and the order is permanently removed from `PendingOrders`, with no path to retry, refund, or otherwise reconcile the failed delivery.

## Impact Explanation
This allows the relayer reward to be paid for deliveries that never actually completed successfully on Ethereum, misallocating the reward/fee budget, and it permanently destroys the `PendingOrder` state for the failed nonce so there is no mechanism left to retry or refund the message — a duplicate/incorrect settlement of bridge reward payout and state, matching the "duplicate settlement or payout" / "runtime bugs that compromise intended behavior" impact categories in scope.

## Likelihood Explanation
Any relayer that observes (or causes, e.g. by underfunding gas for the Gateway-side dispatch) a genuine Ethereum transaction in which the Gateway's execution of a queued command reverts can submit the corresponding real receipt through the verification-gated submission path. Because the underlying Merkle/log proof is authentic, proof verification and the `gateway` check pass; only the unread `success` field distinguishes the outcome. No privileged access, colluding validators, or leaked keys are required — any relayer submitting an honest, unmodified failed-delivery receipt triggers the bug, making it readily and repeatably reachable.

## Recommendation
Branch settlement logic on `receipt.success`:
- If `receipt.success == false`, skip `T::RewardPayment::register_reward`.
- Implement an explicit non-happy-path handling for failed deliveries (e.g., a distinct `MessageDeliveryFailed` event and a defined disposition for the order/fee — refund, retry, or otherwise — rather than unconditionally calling `<PendingOrders<T>>::remove(nonce)` and paying the reward).

## Proof of Concept
1. A message is enqueued via `process_message_impl`, inserting `PendingOrders[nonce]` with `fee > 0` [5](#0-4) .
2. On Ethereum, the Gateway's execution of the corresponding command for that nonce reverts, and the `InboundMessageDispatched` event is emitted with `success: false`.
3. A relayer proves this genuine event log through the verification path, producing a `DeliveryReceipt { gateway, nonce, reward_address, topic, success: false }` via `TryFrom<&Log>`.
4. `process_delivery_receipt` is invoked with this receipt: `T::RewardPayment::register_reward` executes and pays the relayer, and `<PendingOrders<T>>::remove(nonce)` deletes the order — identical to the successful-delivery path, since `success` is never checked [6](#0-5) .
5. A unit test asserting `order.fee` is not registered as a reward when `success: false` (and that the order is either retained or handled distinctly) fails against the current implementation, confirming the missing gate.

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
