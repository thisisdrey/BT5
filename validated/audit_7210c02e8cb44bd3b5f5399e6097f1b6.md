This confirms the claim exactly as described. `process_delivery_receipt` at [1](#0-0)  checks only the gateway address and the presence of a `PendingOrder` by nonce, then unconditionally pays `order.fee` via `T::RewardPayment::register_reward` and removes the order — it never reads `receipt.success`. The `DeliveryReceipt` struct decoded from the Ethereum log does carry a `success: bool` field populated from the `InboundMessageDispatched` event [2](#0-1)  and correctly set during decoding [3](#0-2) , but this value is discarded before it can gate the reward. The pending order is created with a fee at message submission time [4](#0-3) , and the only verification performed on the receipt is authenticity of the log via `T::Verifier::verify`, not the outcome it encodes — a relayer submitting a legitimately-signed proof for a failed dispatch (`success: false`) still collects the full fee and the order is removed with no path to re-flag or retry it.

This matches the required impact category (bridge reward/payout settling incorrectly — paid out despite the underlying condition it is supposed to gate not having occurred), is reachable by an unprivileged relayer submitting ordinary, valid proofs of legitimate on-chain events (no forged proof needed, no privileged actor), and names the exact corrupted value (reward payout / `PendingOrder` removal proceeding regardless of `DeliveryReceipt.success`).

Audit Report

## Title
Relayer reward and order settlement ignore the `DeliveryReceipt.success` field, allowing rewards to be paid for failed message dispatch - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`process_delivery_receipt` pays the pending relayer fee and removes the `PendingOrder` based solely on the presence of a valid, verified event log matching a nonce, without checking whether the decoded `DeliveryReceipt.success` field is `true` or `false`. This allows a relayer to collect the full reward for a message whose Ethereum-side dispatch actually failed.

## Finding Description
The `DeliveryReceipt` decoded from the `InboundMessageDispatched` Ethereum log carries an explicit `success: bool` field, correctly populated during decoding at [3](#0-2) . However, `process_delivery_receipt` at [1](#0-0)  only checks the gateway address (`T::GatewayAddress::get() == receipt.gateway`) and looks up the `PendingOrder` by `receipt.nonce`; it then unconditionally calls `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)` when `order.fee > 0`, and removes the order. `receipt.success` is never read. The verifier (`T::Verifier::verify`) only attests to log authenticity, not to the dispatch outcome, so a fully authentic receipt for a reverted/failed command on Ethereum passes every existing check.

## Impact Explanation
Any relayer that submits a valid proof of an `InboundMessageDispatched` event with `success: false` — which can occur for ordinary reasons such as a reverted command on the Ethereum side — is paid `order.fee` in full, and the `PendingOrder` is removed. This breaks the "settle exactly once to the rightful beneficiary and amount" and "payout state must only advance after ... execution ... succeed" invariants for bridge processing: reward accounting is corrupted because compensation is granted for work that was not actually completed, and the order can never be revisited afterward since it's removed from `PendingOrders`.

## Likelihood Explanation
No forged proof, privileged actor, or malicious relayer collusion is required. Any relayer submitting an authentic proof of a legitimately-failed dispatch event during normal bridge operation triggers this path; the flaw is deterministic and repeatable on every failed-dispatch receipt.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only invoke `T::RewardPayment::register_reward` when `receipt.success == true`. When `success == false`, resolve/remove the order (or handle per intended incident-response design) without granting the fee, and emit a distinct event reflecting the failed outcome.

## Proof of Concept
1. A message is enqueued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }` with `fee > 0` [4](#0-3) .
2. Ethereum's Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` because the dispatched command reverted.
3. A relayer submits `submit_delivery_receipt` with a valid proof of this authentic log; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly [3](#0-2) .
4. `process_delivery_receipt` proceeds unconditionally: `order.fee > 0` triggers `T::RewardPayment::register_reward`, paying the relayer for a message that never succeeded on Ethereum, and `PendingOrders::remove(nonce)` clears the order permanently [5](#0-4) .

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L38-51)
```rust
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
