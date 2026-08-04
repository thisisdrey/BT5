### Title
`process_delivery_receipt` ignores the `success` field and pays the relayer reward even for failed message delivery - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success` flag that records whether the outbound message actually executed successfully on the Ethereum Gateway. `Pallet::process_delivery_receipt` decodes this field but never reads it when deciding whether to reward the relayer — the fee is paid unconditionally as long as `order.fee > 0`, mirroring exactly the bug class in the external report where a configured value (`provider`) is read/stored but never actually consulted, so the code always falls back to a fixed/default behavior (paying the full reward) instead of the value that should gate it (`success`).

### Finding Description
The `DeliveryReceipt` struct decoded from the Ethereum gateway log includes a `success: bool` field: [1](#0-0) 

This field is populated from the `InboundMessageDispatched` Solidity event, which explicitly reports whether the dispatched command failed or succeeded on Ethereum: [2](#0-1) 

However, in `process_delivery_receipt`, only `receipt.gateway` and `receipt.nonce` are checked; `receipt.success` is never inspected before the reward is registered: [3](#0-2) 

Specifically, the reward path is:
```
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
```
There is no `ensure!(receipt.success, ...)` or branch that reduces/withholds the reward when `success == false`. As long as any relayer submits a receipt with a matching `gateway` and `nonce` (which are public, observable on-chain values once a message is committed), the pallet pays the full fee registered in `PendingOrders`, regardless of whether the corresponding commands actually executed on Ethereum. This is structurally identical to the reported bug: a value that exists specifically to select/gate the correct behavior (`provider` in the report, `success` here) is decoded/available but silently ignored, so the contract/pallet always executes the same ("default") path — here, "always pay the reward" instead of "pay only on successful delivery."

### Impact Explanation
This breaks the intended settlement invariant that a Snowbridge relayer reward should only be paid for a *successfully delivered* message. A failed dispatch on Ethereum (e.g., due to a reverting embedded call, insufficient gas provided by the relayer, or a malformed command) still results in the relayer draining `order.fee` from the reward pool, since `PendingOrders` is removed and the reward registered unconditionally. This is a case of unbacked/unwarranted payout: value leaves the system to a party that did not deliver the promised service, degrading the queue's economic model and allowing relayers to be rewarded for public "work" that did not actually succeed.

### Likelihood Explanation
Likelihood is high because triggering `success = false` requires no special privilege — any of the outbound commands dispatched via the Gateway can revert for reasons outside the relayer's control (e.g., insufficient gas headroom, external contract state changes), and a relayer only needs to submit the delivery receipt through the ordinary, permissionless `process_delivery_receipt` flow with a genuine nonce/gateway match. No malicious peer, admin, or governance action is required.

### Recommendation
Gate the reward payment on `receipt.success`:
```rust
if order.fee > 0 && receipt.success {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
```
Optionally emit a distinct event (e.g. `MessageDeliveryFailed`) when `success == false`, and decide policy for the unpaid fee (refund to sender, retry, or burn) instead of silently paying it out regardless of outcome.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = PendingOrder { nonce, fee, block_number }` with `fee > 0`. [4](#0-3) 
2. The commands committed for that nonce fail to execute on the Ethereum Gateway (e.g., a call reverts), causing the Gateway to emit `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer observes this event, constructs a valid `DeliveryReceipt` (gateway and nonce match the pallet's `GatewayAddress` and pending order), and submits it via the delivery-receipt call path leading to `process_delivery_receipt`.
4. `ensure!(T::GatewayAddress::get() == receipt.gateway, ...)` passes; `PendingOrders::get(nonce)` returns `Some(order)` with `order.fee > 0`; since `receipt.success` is never checked, `T::RewardPayment::register_reward` is invoked and the relayer is credited the full fee even though delivery failed.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-12)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}
```

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
