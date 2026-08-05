This confirms the claim exactly as described. The code at `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` lines 445-480 shows `process_delivery_receipt` never references `receipt.success` anywhere in its logic — it only checks `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce`, then unconditionally pays the reward (gated only on `order.fee > 0`) and removes the `PendingOrders` entry.Confirmed: the `success` field is decoded from the Ethereum event but never referenced in `process_delivery_receipt` — no branch, guard, or conditional exists on it anywhere in the pallet.

Audit Report

## Title
`process_delivery_receipt` pays relayer reward and settles the pending order without checking the `DeliveryReceipt.success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` in the Snowbridge V2 outbound queue pallet gates the relayer reward payout and `PendingOrders` removal solely on the existence of a `PendingOrders` entry for `receipt.nonce`, never inspecting `receipt.success`. This field is decoded straight from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event and is meant to indicate whether the destination-side dispatch on Ethereum actually succeeded, but it is unused after decoding.

## Finding Description
`DeliveryReceipt::try_from(&Log)` decodes `success` directly from the verified Ethereum log [1](#0-0) , and the struct explicitly documents this field as "Delivery status" [2](#0-1) . However, `process_delivery_receipt` only checks `receipt.gateway`, derives `reward_account` from `receipt.reward_address`, looks up the `PendingOrders` entry by `receipt.nonce`, and — gated only on `order.fee > 0` — unconditionally calls `T::RewardPayment::register_reward`, then removes the order and emits `MessageDelivered`: [3](#0-2) . A grep across the pallet confirms `receipt.success` is never referenced anywhere in the settlement logic. The existing guard (does the nonce have a `PendingOrders` entry) is coarse and does not distinguish a genuinely successful Ethereum dispatch from a reverted one — both produce a verifier-passing log and identical decoded `DeliveryReceipt` shape aside from the `success` bit, and both take the same code path to reward + settle.

## Impact Explanation
This allows a relayer to be paid `order.fee` via `T::RewardPayment::register_reward` and have the `PendingOrders` entry for that nonce permanently removed (with `MessageDelivered` emitted) even when the corresponding Ethereum-side dispatch reverted (`success == false`). Since `PendingOrders` is the only state tracking this message's delivery status and there is no separate failure-tracking or retry mechanism, this is a duplicate/incorrect settlement: reward funds sourced from the shared reward pool (`pallet-bridge-relayers` reward ledger) are paid for work that did not achieve its intended effect, and the message is irreversibly marked delivered despite failing on the destination chain. This matches the "duplicate settlement or payout" and value-conservation impact categories in the accepted gate.

## Likelihood Explanation
No privileged or malicious behavior is required. `submit_delivery_receipt` is a plain signed extrinsic open to any account, and any relayer submitting a truthful, verifier-passing proof for a real `InboundMessageDispatched` event with `success == false` (e.g., destination call reverted, out-of-gas, insufficient allowance) triggers this path automatically. Such destination-side failures are a normal, expected occurrence in cross-chain messaging, not a contrived edge case, making this readily and repeatably triggerable in honest operation.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before paying the reward and/or removing the `PendingOrders` entry:
- If `receipt.success == false`, skip `T::RewardPayment::register_reward` (or pay a distinct, reduced amount per protocol design) and emit a distinct event (e.g., `MessageDeliveryFailed { nonce }`) instead of `MessageDelivered`.
- Consider retaining failed-delivery records for retry/governance handling rather than deleting `PendingOrders` unconditionally, so a failed dispatch is not silently treated as final successful settlement.

## Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrders` entry with non-zero `fee` [4](#0-3) .
2. On Ethereum, the Gateway's dispatch to the final destination reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid proof of this real event and calls `submit_delivery_receipt`; verification passes (log is genuine) and it decodes to `DeliveryReceipt { success: false, .. }`.
4. `process_delivery_receipt` finds the `PendingOrders` entry, unconditionally registers the reward, removes the order, and emits `MessageDelivered` — identical to the success path.
5. A unit test can reproduce this: insert a `PendingOrder` with `fee > 0`, construct a `DeliveryReceipt` with `success: false`, call `process_delivery_receipt`, and observe that `RewardPayment::register_reward` is still invoked and `PendingOrders::get(nonce)` returns `None` afterward — demonstrating the payout and settlement occur independent of the `success` flag's value.

### Citations

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
