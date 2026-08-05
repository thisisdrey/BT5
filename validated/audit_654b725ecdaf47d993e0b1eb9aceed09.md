Confirmed: `receipt.success` is decoded from the Ethereum event log but is never referenced anywhere else in the codebase besides its definition. This confirms the claim exactly matches the actual code in `process_delivery_receipt`.

Audit Report

## Title
Relayer reward is paid regardless of delivery outcome (`success` flag ignored) - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`process_delivery_receipt` in the Outbound Queue V2 pallet decodes a `DeliveryReceipt` from a verified Ethereum event log, including a `success: bool` field indicating whether the message dispatch on Ethereum actually succeeded, but never checks this field before paying out the relayer reward via `T::RewardPayment::register_reward`. Any signed account can submit a `submit_delivery_receipt` extrinsic with a legitimately-verified proof for a failed dispatch and still receive the full fee.

## Finding Description
The `InboundMessageDispatched` event carries a `success` flag decoded into `DeliveryReceipt.success` [1](#0-0) . The extrinsic `submit_delivery_receipt` verifies the proof via `T::Verifier::verify`, decodes the receipt, and forwards it to `process_delivery_receipt` [2](#0-1) . Inside `process_delivery_receipt`, the `PendingOrder` is fetched by `receipt.nonce`, and if `order.fee > 0`, the reward is unconditionally registered via `T::RewardPayment::register_reward` — `receipt.success` is never read anywhere in this function [3](#0-2) . A grep across the whole `bridges/snowbridge` tree confirms `receipt.success`/`.success` is referenced only at its point of definition/decoding, and nowhere in the consumption path. The pallet's own documentation states the reward should only be paid "when the message has been verified and executed" [4](#0-3) , confirming this is a deviation from intended behavior rather than a deliberate design choice.

## Impact Explanation
This breaks the "settle exactly once to the rightful beneficiary and amount" invariant named in the impact gate: `T::RewardPayment::register_reward` pays out `order.fee` to `reward_account` regardless of whether the corresponding Ethereum-side dispatch actually succeeded. The corrupted value is the reward payout amount/condition — it is decoupled from the `receipt.success` outcome it is supposed to be gated on. This allows systematic drain of the reward/fee pool paid out of the account funding fees, for deliveries that provide no value (failed dispatches), which is a form of theft/unbacked payout as covered by the Polkadot SDK impact gate.

## Likelihood Explanation
No privileged actor, governance action, or compromised relayer/prover assumption is needed. Any unprivileged, signed account can call the public extrinsic `submit_delivery_receipt` with a legitimately verified proof for a naturally-occurring or deliberately engineered failed Ethereum dispatch (e.g., a command whose gas/target guarantees revert). Because the `success` check is completely absent from the code path, the bug triggers deterministically on every call where `order.fee > 0` and the corresponding dispatch failed.

## Recommendation
Gate the reward payment in `process_delivery_receipt` on `receipt.success`:
```rust
if receipt.success && order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
```
Still remove the `PendingOrder` regardless of outcome to avoid permanently stuck pending orders, and consider emitting a distinct event for failed deliveries.

## Proof of Concept
1. Any account sends an outbound XCM message causing `do_process_message` to create a `PendingOrder{nonce, fee, ..}` with `fee > 0` [5](#0-4) .
2. The corresponding command dispatch fails on Ethereum, so the emitted `InboundMessageDispatched` event log has `success = false`.
3. Any signed account submits `submit_delivery_receipt` with the legitimate proof/log for that failed dispatch — `T::Verifier::verify` succeeds since the proof itself is valid, only the dispatch outcome is a failure.
4. `process_delivery_receipt` pays the reward via `T::RewardPayment::register_reward` and removes the `PendingOrder` identically to the success case, because `receipt.success` is never consulted [6](#0-5) .

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
