Audit Report

## Title
`process_delivery_receipt` pays relayer reward without checking `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
The `DeliveryReceipt` struct decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field indicating whether the dispatched command actually succeeded on Ethereum, but `Pallet::process_delivery_receipt` never reads or branches on this field before paying out the relayer reward and clearing the pending order. Any relayer submitting a valid proof of a genuinely failed dispatch (`success = false`) still receives the fee reward as if delivery had succeeded.

## Finding Description
The event and its decoded receipt preserve a `success` field populated directly from the on-chain Ethereum log: [1](#0-0) [2](#0-1) 

The extrinsic entry point `submit_delivery_receipt` verifies the proof, decodes the receipt, and immediately forwards it to `process_delivery_receipt` with no success gating in between: [3](#0-2) 

`process_delivery_receipt` itself only checks the gateway address matches and that a `PendingOrder` exists for the nonce, then unconditionally registers the reward when `order.fee > 0` and removes the order — `receipt.success` is never read anywhere in the function body: [4](#0-3) 

The module doc comment describing the intended flow ("pay reward with fee attached in the order") makes no mention of conditioning payment on dispatch success, and the field is fully decoded but discarded — confirming the guard is simply absent rather than intentionally omitted.

## Impact Explanation
Because `receipt.success` is ignored, `T::RewardPayment::register_reward` is invoked and the `PendingOrder` is removed for `order.fee` regardless of whether the underlying Ethereum-side command dispatch actually succeeded. This is a payout that does not correspond to the bridge action actually completing — an "unbacked"/duplicate settlement scenario matching the Impact Gate's "theft or unbacked mint or unlock" / "duplicate settlement or payout" criteria, reachable without any privileged actor, forged proof, or relayer collusion.

## Likelihood Explanation
`submit_delivery_receipt` is a public, permissionless, signed extrinsic reachable by any account acting as relayer, as shown by `ensure_signed(origin)?` with no further authorization check: [5](#0-4) 
An attacker only needs to submit a legitimately verifiable Ethereum event log where the dispatched command reverted (`success = false`) — a naturally occurring condition (e.g., insufficient allocated gas, downstream contract revert), not requiring proof forgery.

## Recommendation
Add an explicit check on `receipt.success` in `process_delivery_receipt` before calling `T::RewardPayment::register_reward` — e.g., only pay the fee reward when `receipt.success == true`; otherwise emit a distinct failure event and/or apply an alternate reward/penalty policy, while still removing the stale `PendingOrder` entry.

## Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }`: [6](#0-5) 
2. The message is relayed to Ethereum but the dispatched command reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any account acting as relayer calls `submit_delivery_receipt` with a valid proof of this failed event; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly.
4. `process_delivery_receipt` checks only gateway match and `PendingOrders` existence — both pass — then unconditionally calls `T::RewardPayment::register_reward(&reward_account, .., order.fee)` and removes the order, paying the relayer despite the failed dispatch: [7](#0-6)

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
