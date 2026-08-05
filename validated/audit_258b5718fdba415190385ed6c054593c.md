The claim is fully confirmed by the code. `process_delivery_receipt` at [1](#0-0)  decodes `receipt` (which contains the `success: bool` field per [2](#0-1) ) but never reads or branches on `receipt.success` — it only checks `T::GatewayAddress::get() == receipt.gateway` and `PendingOrders::get(nonce).is_some()`, then unconditionally pays the reward via `T::RewardPayment::register_reward` when `order.fee > 0` and unconditionally removes the order with `<PendingOrders<T>>::remove(nonce)`. `submit_delivery_receipt` at [3](#0-2)  is a public, permissionless extrinsic that verifies the proof and calls this function directly.

Audit Report

## Title
Relayer reward paid and pending order settled regardless of on-chain delivery outcome (`receipt.success` never checked) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`submit_delivery_receipt` decodes a `DeliveryReceipt` containing a verified `success: bool` field indicating whether the Ethereum-side command dispatch actually succeeded, but `process_delivery_receipt` never inspects `receipt.success` before paying the relayer reward and clearing the pending order. This allows a relayer to collect the full fee and have the order settled for messages that genuinely failed to execute on Ethereum, as long as a valid delivery-receipt log/proof exists.

## Finding Description
`submit_delivery_receipt` verifies the event log/proof via `T::Verifier::verify` and decodes it into `DeliveryReceipt` via `DeliveryReceipt::try_from`, then forwards it to `process_delivery_receipt` without any success check. `DeliveryReceipt` carries `success: bool` sourced directly from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event. In `process_delivery_receipt`, the only guards applied are: (1) `T::GatewayAddress::get() == receipt.gateway`, and (2) existence of `PendingOrders::get(nonce)`. Neither of these validates that the message dispatch actually succeeded on the destination chain. The function then unconditionally calls `T::RewardPayment::register_reward` when `order.fee > 0`, and unconditionally removes the pending order via `<PendingOrders<T>>::remove(nonce)`, regardless of the value of `receipt.success`.

## Impact Explanation
This matches the "duplicate settlement or payout" / "public underpriced work" impact category: bridge relayer rewards are paid and pending-order/bridge-state tracking is advanced without the intended precondition (successful execution on Ethereum) being satisfied. A relayer submitting a legitimate proof for a failed dispatch (`success=false`) receives full payment and the order is cleared exactly as though delivery succeeded, corrupting the reward/payout state's correctness guarantee tied to `PendingOrders[nonce]` and the `order.fee` payout.

## Likelihood Explanation
This is triggerable by any unprivileged relayer submitting a valid delivery-receipt proof for a message that failed on Ethereum — a routine, non-malicious occurrence requiring no special privileges, key compromise, or governance action. The extrinsic `submit_delivery_receipt` is public and permissionless.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only invoke `T::RewardPayment::register_reward` when `receipt.success == true`. For `success == false`, still remove the `PendingOrder` to avoid indefinite buildup, but emit a distinct event (e.g., `MessageDeliveryFailed`) and withhold or reduce the reward instead of paying it in full.

## Proof of Concept
1. `do_process_message` inserts `PendingOrders[nonce] = PendingOrder { fee: F, .. }` for an outbound message.
2. The Ethereum Gateway processes the message but the commands revert, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits this genuine log and proof via `submit_delivery_receipt`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success=false` correctly.
4. `process_delivery_receipt` never reads `receipt.success`; since `order.fee > 0`, it calls `T::RewardPayment::register_reward` for the full fee and removes the pending order, identical to the success path — as confirmed by the existing test `submit_delivery_receipt_succeeds_after_unhalt`, which never varies `success` yet still results in reward payment and order removal.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L300-317)
```rust
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
