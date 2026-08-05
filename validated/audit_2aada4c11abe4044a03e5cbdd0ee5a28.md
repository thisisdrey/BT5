Audit Report

## Title
Relayer reward paid regardless of message dispatch outcome due to unchecked `success` field - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`process_delivery_receipt` pays out the relayer's `order.fee` and permanently removes the `PendingOrder` for a nonce based solely on gateway-address match and the existence of a `PendingOrder` entry, without ever inspecting the `receipt.success` field that is decoded from the Ethereum `InboundMessageDispatched` event. This allows a relayer to collect the reward and settle the order even when the corresponding message dispatch failed on the Ethereum side.

## Finding Description
The Ethereum Gateway emits `InboundMessageDispatched(nonce, topic, success, reward_address)` where `success` reflects whether the dispatched command actually executed successfully on Ethereum. [1](#0-0) 

This field is decoded into `DeliveryReceipt::success` via `TryFrom<&Log>`: [2](#0-1) 

However, `process_delivery_receipt` only checks `receipt.gateway` against the configured `GatewayAddress` and whether a `PendingOrder` exists for `receipt.nonce`. It never reads `receipt.success` before calling `T::RewardPayment::register_reward` and removing the order: [3](#0-2) 

The `PendingOrder` is created with the fee amount when the message is enqueued, independent of any later dispatch outcome: [4](#0-3) 

I confirmed via `grep_search` that `receipt.success` is never referenced anywhere else in `outbound-queue-v2/src/lib.rs`, and the existing test suite (`test.rs`) only exercises the halted-verifier and success-after-unhalt paths — no test asserts that a receipt with `success = false` is rejected or handled differently from a successful one. The only verification performed prior to settlement (`T::Verifier::verify`, invoked in `submit_delivery_receipt`) checks proof-of-inclusion of the event log, not the semantic outcome carried inside that log's `success` field.

## Impact Explanation
This violates the intended settlement invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Because `success` is decoded but never checked, `process_delivery_receipt` treats failed and successful dispatches identically: it pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward` and calls `<PendingOrders<T>>::remove(nonce)`, permanently foreclosing any distinct accounting or reprocessing for that nonce. This is a reward/settlement-correctness defect — value (relayer_fee) is disbursed without the successful work condition the system is designed to require — even though it does not directly drain bridge-held principal funds.

## Likelihood Explanation
The exploit path requires only a standard signed extrinsic call to the public `submit_delivery_receipt` and a legitimately verifiable Merkle/execution proof for a genuinely emitted (but failed, `success = false`) `InboundMessageDispatched` event — no privileged role, governance action, or compromised key is needed. Any relayer submitting a valid proof for a nonce whose Ethereum-side command execution failed can collect the fee unconditionally, since `T::Verifier::verify` only attests proof validity, not command outcome, and `process_delivery_receipt` performs no additional gating on `receipt.success`.

## Recommendation
In `process_delivery_receipt` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`), branch on `receipt.success`: only invoke `T::RewardPayment::register_reward` when `receipt.success == true`. For `success == false`, still remove/mark the `PendingOrder` to prevent nonce replay, but emit a distinct event (e.g. `MessageDeliveryFailed`) and skip the reward payment, making delivery success a first-class condition of settlement.

## Proof of Concept
1. A message is enqueued via `do_process_message`, inserting `PendingOrders::<T>::insert(nonce, order)` with `order.fee > 0`.
2. On Ethereum, the corresponding command dispatch fails inside the Gateway, which emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid receipt/execution proof for this event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the event genuinely occurred and is provably included) and `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` ignores this value, unconditionally pays `order.fee` to the relayer, and removes the `PendingOrder`, as confirmed by reading the function body directly — there is no `if receipt.success` check anywhere in the pallet.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-12)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
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
