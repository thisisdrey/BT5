I found a concrete local analog: `DeliveryReceipt` decodes a `success: bool` field from the Ethereum `InboundMessageDispatched` event, but `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` never reads or checks that field before paying the relayer reward.

### Title
Relayer reward paid regardless of message dispatch outcome due to unchecked `success` field - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The `submit_delivery_receipt` extrinsic verifies the Ethereum proof and gateway address, then calls `process_delivery_receipt`, which pays out the relayer fee from `PendingOrders` purely based on the existence of a matching nonce — without inspecting the `success` flag that is explicitly decoded into `DeliveryReceipt`.

### Finding Description
The Ethereum-side `InboundMessageDispatched` event carries a `success: bool` field indicating whether the dispatched command actually executed successfully on Ethereum: [1](#0-0) 

This field is faithfully decoded into the `DeliveryReceipt` struct's `success` member: [2](#0-1) 

However, `process_delivery_receipt` on the BridgeHub side only checks the gateway address and whether a `PendingOrder` exists for the nonce; it never reads `receipt.success` before unconditionally paying the fee and removing the order: [3](#0-2) 

Because the field is present in the type but structurally unused, any relayer that submits a valid Merkle/receipt proof for a nonce (regardless of whether the dispatched command reverted or failed on the Ethereum Gateway) collects the full `order.fee` reward, and the order is deleted from `PendingOrders` — preventing any future re-processing or accounting correction for that nonce.

### Impact Explanation
This breaks the intended settlement invariant that rewards should only be paid for **successful** message delivery. An unprivileged relayer (not a "malicious validator/collator/admin" — just any signed account submitting a legitimate Ethereum receipt proof) can:
- Deliberately cause or exploit a message that fails execution on Ethereum (e.g., a command that reverts inside `InboundMessageDispatched` handling), then still claim the relayer_fee reward via `submit_delivery_receipt`, since `T::Verifier::verify` only checks proof validity, not command outcome.
- Permanently consume the `PendingOrders` entry, meaning the pallet's own order/fee bookkeeping treats a failed delivery identically to a successful one, with no distinct on-chain signal for downstream systems (e.g., relayer reputation, retry/dispute logic) to detect failure.

This is a reward/accounting-conservation violation (fee paid without commensurate successful work) rather than a full fund-drain, but it matches the report's pattern of a component ("outbound-queue-v2") that trusts a lower-level signal (proof of *an event occurred*) without validating the actual semantic outcome (*did the operation succeed*) that the reward is meant to be conditioned on.

### Likelihood Explanation
High likelihood of exploitation in practice: any relayer can pick nonces whose Ethereum-side commands are known/likely to fail (e.g. underfunded gas, reverting command payloads) and still collect the fee, since nothing in `process_delivery_receipt` or `submit_delivery_receipt` gates on `success`. No privileged role, governance action, or compromised key is required — only a standard signed extrinsic call with a legitimately verifiable proof.

### Recommendation
In `process_delivery_receipt` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`), branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`. For failed deliveries, consider a separate accounting path (e.g., still remove/mark the `PendingOrder` to prevent replay, but emit a distinct `MessageDeliveryFailed` event and skip reward payment), so success is a first-class condition of settlement rather than a decoded-but-unused field.

### Proof of Concept
1. A message is enqueued via `do_process_message`, creating `PendingOrders::<T>::insert(nonce, order)` with `order.fee > 0`. [4](#0-3) 
2. On Ethereum, the corresponding command dispatch reverts/fails inside the Gateway, causing the Gateway to emit `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains the valid receipt/execution proof for this (failed) event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the event genuinely occurred and is provably included), `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` ignores this and pays `order.fee` to the relayer regardless, then deletes the `PendingOrder`. [5](#0-4) 

Note: I could not fully verify from the index whether any downstream consumer (e.g., runtime-level fee accounting or a governance dispute mechanism) compensates for this gap elsewhere in the runtime wiring; the pallet-level logic itself does not check `success`, which is the concrete, directly-provable local analog to the report's "trusts without validation" pattern.

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
