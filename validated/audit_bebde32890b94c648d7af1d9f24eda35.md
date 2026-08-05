The code confirms the claim exactly as described. `DeliveryReceipt.success` is decoded from the on-chain `InboundMessageDispatched` event <cite repo="Loderfordw/polkadot-sdk--031" path="bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs" start="10-12" end="44-51" />, but `process_delivery_receipt` never references `receipt.success` anywhere in its logic — it only checks `receipt.gateway`, looks up `PendingOrders`, and pays the reward whenever `order.fee > 0`, unconditionally [1](#0-0) .

Audit Report

## Title
`process_delivery_receipt` ignores the `success` field and pays the relayer reward even for failed message delivery - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`DeliveryReceipt` carries a `success: bool` flag decoded from the Ethereum `InboundMessageDispatched` event, indicating whether the outbound commands actually executed successfully on the Gateway contract. `Pallet::process_delivery_receipt` decodes and holds this field but never inspects it before calling `T::RewardPayment::register_reward`, so the relayer reward for `order.fee` is paid whenever `order.fee > 0`, regardless of whether Ethereum-side execution succeeded or failed.

## Finding Description
The `DeliveryReceipt` struct populated via `TryFrom<&Log>` sets `success: event.success` straight from the Solidity `InboundMessageDispatched(nonce, topic, bool success, reward_address)` event [2](#0-1) . In `process_delivery_receipt`, the guards applied are only `ensure!(T::GatewayAddress::get() == receipt.gateway, ...)` and `<PendingOrders<T>>::get(nonce).ok_or(...)`; the reward path `if order.fee > 0 { T::RewardPayment::register_reward(...) }` never reads `receipt.success` [3](#0-2) . The pending order is created with a `fee` field, independent of any later delivery outcome [4](#0-3) . Thus a relayer that submits a receipt reporting `success = false` for a genuinely failed dispatch on Ethereum still receives the full `order.fee` reward, because nothing in the function branches on that value.

## Impact Explanation
This is an unbacked/unwarranted payout: the reward pool pays out `order.fee` to a relayer account for work (message delivery) that did not actually succeed, since the settlement of the pending order is not conditioned on successful execution. This matches the "theft or unbacked mint/unlock" / "duplicate settlement or payout" class of impact for bridge reward pallets, since value leaves the reward pool without the corresponding service having been rendered.

## Likelihood Explanation
Any permissionless relayer submitting a message-delivery-receipt proof through the ordinary flow can trigger this: they need only observe a genuine `InboundMessageDispatched` event with `success = false` (which can occur naturally due to reverts in the dispatched commands, unrelated to relayer misbehavior) and forward it. No privileged role, governance action, or malicious peer/relayer collusion is required — the relayer is simply following normal reward-claiming behavior with an event that legitimately reports failure.

## Recommendation
Condition the reward payment on `receipt.success`, e.g.:
```rust
if order.fee > 0 && receipt.success {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
```
Consider emitting a distinct event on failed delivery and defining explicit policy (refund, retry, or forfeiture) for the fee tied to a failed order instead of silently paying it in either case.

## Proof of Concept
1. `do_process_message` inserts `PendingOrders[nonce] = PendingOrder { nonce, fee: F, block_number }` with `F > 0` [5](#0-4) .
2. The corresponding commands revert on the Ethereum Gateway, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits a `DeliveryReceipt` with `gateway` matching `T::GatewayAddress`, `nonce` matching the pending order, and `success = false` via the extrinsic path invoking `process_delivery_receipt`.
4. `ensure!(T::GatewayAddress::get() == receipt.gateway, ...)` passes; `PendingOrders::get(nonce)` returns `Some(order)` with `order.fee = F > 0`; since `receipt.success` is never checked, `T::RewardPayment::register_reward(&reward_account, ..., F)` executes and the relayer is credited `F` despite `success == false`.

### Citations

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
