Audit Report

## Title
`process_delivery_receipt` in Snowbridge Outbound Queue V2 ignores `DeliveryReceipt.success`, rewarding relayers for failed message dispatch - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` that carries a `success: bool` field indicating whether the corresponding message dispatch on the Ethereum Gateway succeeded or reverted, but the function never inspects this field before paying the relayer's fee and clearing the `PendingOrder`. As a result, a relayer submitting a proof for a genuinely failed/reverted delivery is paid exactly as if delivery succeeded.

## Finding Description
`DeliveryReceipt` is decoded from the `InboundMessageDispatched(nonce, topic, success, reward_address)` event log emitted by the Ethereum Gateway, and explicitly includes a "Delivery status" field named `success`. [1](#0-0) 

The `TryFrom<&Log>` conversion faithfully populates `success` from the decoded on-chain event, so this value reflects the actual dispatch outcome on Ethereum and cannot be tampered with independently of the underlying proof. [2](#0-1) 

`process_delivery_receipt` only checks the gateway address, resolves the reward account, looks up the `PendingOrder` by nonce, pays the reward if `order.fee > 0`, removes the order, and emits `MessageDelivered` — at no point does it reference `receipt.success`: [3](#0-2) 

A grep across the entire `outbound-queue-v2` pallet source confirms there is no other check on `success` anywhere in the crate, so no compensating control exists elsewhere in the settlement path.

## Impact Explanation
This matches the impact-gate category "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Because payout and order removal (`PendingOrders::<T>::remove(nonce)`) proceed unconditionally once the gateway and nonce match, a relayer is paid `order.fee` and the order is permanently settled as "delivered" even when the actual Ethereum-side command execution failed (`success == false`). This decouples the reward payout from actual successful bridge delivery, misallocating the `fee` value that should only be paid for successful delivery.

## Likelihood Explanation
Any permissionless relayer submitting a legitimate proof for a real Ethereum transaction in which the Gateway's command dispatch fails (e.g., insufficient gas relative to the destination command, or a command that reverts at execution time) but the outer transaction still emits `InboundMessageDispatched(..., success=false, ...)` can trigger this. No collusion, privileged access, or malicious peer/validator behavior is required — this is a straightforward call flow available to any external actor holding a valid receipt proof, satisfying the "unprivileged external attacker using public extrinsics/proof submission" requirement.

## Recommendation
Add an explicit check on `receipt.success` in `process_delivery_receipt` before paying out the reward and removing the order:
```rust
ensure!(receipt.success, Error::<T>::MessageDispatchFailed);
```
If partial/gas-only compensation for failed-but-attempted delivery is an intended policy, implement it as a distinct, separately reasoned code path (e.g., reduced fee) rather than silently paying the full fee via the same "success" path.

## Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrder { nonce, fee, .. }`. [4](#0-3) 
2. A relayer delivers the message to the Ethereum Gateway, but the inner command dispatch reverts; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer obtains a valid Merkle/receipt proof for this event; `TryFrom<&Log>` decodes it into `DeliveryReceipt { success: false, .. }`. [5](#0-4) 
4. `process_delivery_receipt` validates only `gateway` and `nonce`, never `receipt.success`, and unconditionally calls `T::RewardPayment::register_reward` with `order.fee`, then removes the `PendingOrder`. [6](#0-5) 
5. Result: the relayer is fully rewarded and the order is settled as delivered despite the underlying dispatch having failed on Ethereum — reproducible as a unit test that constructs a `DeliveryReceipt` with `success: false` for an existing `PendingOrder` and asserts that `register_reward` is still invoked and the order removed.

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
