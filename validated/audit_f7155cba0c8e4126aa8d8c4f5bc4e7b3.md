The code confirms the claim exactly as described. `process_delivery_receipt` decodes `receipt.success` via the `DeliveryReceipt` struct but never reads or branches on it — it only checks `receipt.gateway`, looks up the `PendingOrder` by `receipt.nonce`, unconditionally calls `T::RewardPayment::register_reward` when `order.fee > 0`, and removes the order, regardless of whether the Ethereum-side execution actually succeeded.This confirms the full chain: `DeliveryReceipt::try_from` at [1](#0-0)  correctly decodes `success: event.success` from the Ethereum `InboundMessageDispatched` event into the `DeliveryReceipt.success` field, but `process_delivery_receipt` never reads that field before paying the reward and removing the pending order.

Audit Report

## Title
`process_delivery_receipt` pays relayer reward and settles the pending order regardless of the Ethereum-side `success` flag - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`DeliveryReceipt` carries a `success: bool` field decoded directly from the Ethereum `InboundMessageDispatched` event, indicating whether the relayed message actually executed successfully on the Gateway contract. `Pallet::process_delivery_receipt` verifies only the gateway address and the existence of a `PendingOrder`, then unconditionally calls `T::RewardPayment::register_reward` and removes the order — `receipt.success` is decoded but never inspected or branched on.

## Finding Description
`DeliveryReceipt` is derived from `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` and its `TryFrom<&Log>` impl explicitly copies `success: event.success` into the struct [2](#0-1) [1](#0-0) .

`submit_delivery_receipt` verifies the proof, decodes the receipt via `DeliveryReceipt::try_from`, and forwards it to `process_delivery_receipt` [3](#0-2) .

`process_delivery_receipt` checks only `T::GatewayAddress::get() == receipt.gateway` and that `PendingOrders::<T>::get(nonce)` exists; it computes `reward_account`, then unconditionally calls `T::RewardPayment::register_reward` whenever `order.fee > 0`, followed by removing the `PendingOrder` and emitting `MessageDelivered` — at no point is `receipt.success` read [4](#0-3) . A repository-wide search confirms no source, test, or benchmark inside `outbound-queue-v2` ever references `receipt.success` beyond its decoding in the primitives crate.

This breaks the invariant, stated in the module's own documentation, that reward payment should occur "when the message has been verified and executed" [5](#0-4) . The existing guards (`Verifier::verify`, gateway-address check, `PendingOrders` existence check) validate proof authenticity and message identity but do nothing to validate delivery outcome, so they are insufficient to prevent settlement on a failed execution.

## Impact Explanation
This is a bridge reward/settlement pallet directly handling value via `T::RewardPayment::register_reward`. Because `success` is ignored, a message whose execution reverted or failed on the Ethereum Gateway (e.g., ran out of gas, hit invalid state) still results in the relayer being paid the full `order.fee` and the `PendingOrder` being cleared as if delivery succeeded — the wrong value here being the `PendingOrder` settlement outcome and reward amount being determined independent of the corrupted/false `receipt.success` flag. This matches the "bridge rewards ... must ... settle exactly once to the rightful beneficiary and amount" pivot, and constitutes theft/unbacked payout from the bridge's perspective: relayers are rewarded for deliveries that did not actually succeed.

## Likelihood Explanation
Likelihood is Medium: any relayer that submits a message to the Ethereum Gateway can subsequently call the public, unprivileged `submit_delivery_receipt` extrinsic with a genuine, chain-verified event proof — no forgery of the proof or event is required, since `T::Verifier::verify` still checks header/merkle validity. The relayer only needs the underlying Ethereum transaction to have emitted `success=false`, a normal outcome of gas-limited or reverting command execution rather than an attacker-controlled input, and the extrinsic remains fully payable and callable by any signed account with no additional privilege.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; on `false`, remove/settle the `PendingOrder` without paying the reward (or route to a distinct failure-handling/refund path), and emit a distinct event (e.g., `MessageDeliveryFailed`) so failed deliveries are auditable instead of being silently rewarded as if successful.

## Proof of Concept
1. A relayer relays an outbound message to the Ethereum Gateway; execution of the message's commands fails/reverts on-chain, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
2. The relayer obtains a valid header/merkle proof for this genuine event and calls `submit_delivery_receipt(origin, event)` on BridgeHub, constructing an `EventProof` whose decoded `DeliveryReceipt.success` is `false`.
3. `T::Verifier::verify` succeeds, `receipt.gateway` matches `T::GatewayAddress`, and `PendingOrders::<T>::get(nonce)` returns the existing order — none of these checks reference `success`.
4. `process_delivery_receipt` proceeds through [6](#0-5)  unconditionally, calling `register_reward` and removing the order — identical behavior to the `success: true` case, confirming the reward is paid and the order settled even though the bridged message never executed successfully on Ethereum.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L44-50)
```rust
		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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
