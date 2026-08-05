The code matches the claim exactly. `process_delivery_receipt` decodes `receipt.success` into the `DeliveryReceipt` struct but never reads or checks it before paying the relayer reward via `T::RewardPayment::register_reward` and removing the `PendingOrder`.Confirmed: `success` is never referenced anywhere in the pallet's logic or test suite. The pallet only checks `receipt.gateway` and the existence of a `PendingOrder` by `nonce`, unconditionally paying the reward and removing the order regardless of the decoded execution outcome.All evidence confirms the claim is accurate and reproducible in the codebase.

Audit Report

## Title
`process_delivery_receipt` pays relayer reward without checking the decoded `success` flag of the delivery receipt - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`DeliveryReceipt::try_from` decodes an Ethereum `InboundMessageDispatched` event log into a struct carrying a `success: bool` field indicating whether the dispatched command actually executed successfully on the Gateway contract. `Pallet::process_delivery_receipt` never inspects `receipt.success` before calling `T::RewardPayment::register_reward` and removing the `PendingOrder`, so a relayer can be paid the full fee and have the order closed out for a message whose execution reverted on Ethereum.

## Finding Description
`DeliveryReceipt` is decoded from a genuine, Merkle/event-proved Ethereum log and explicitly carries `success`: [1](#0-0) . The decoding logic in `TryFrom<&Log>` faithfully copies `event.success` into the struct without discarding it: [2](#0-1) .

`submit_delivery_receipt` verifies the proof via `T::Verifier::verify`, decodes the receipt, and forwards the entire `receipt` (including `success`) to `process_delivery_receipt`: [3](#0-2) .

`process_delivery_receipt` only checks `receipt.gateway` against `T::GatewayAddress` and looks up the `PendingOrder` by `receipt.nonce`; it never reads `receipt.success` before paying the reward and removing the order: [4](#0-3) . A repository-wide search confirms `success` is decoded but never referenced anywhere else in the pallet's logic or its test suite, so no guard exists that gates payout/order-removal on actual execution success.

The module's own doc-header states the intended flow: "When the message has been verified **and executed**, the relayer will call ... to ... pay reward" [5](#0-4) , confirming that reward payment is meant to be conditional on successful execution — a condition the code does not enforce.

## Impact Explanation
Any relayer submitting a genuine `InboundMessageDispatched` log with `success == false` (a normal outcome when Gateway dispatch reverts, e.g., due to gas limits or command failure) still collects the full relayer fee via `T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee)`, and the corresponding `PendingOrder` is unconditionally removed from `PendingOrders`, closing out the bookkeeping as if delivery succeeded. This is a duplicate/incorrect settlement and underpriced-work issue: rewards are paid for work that did not achieve its intended effect on Ethereum, and the pending-order state that should track outstanding/failed deliveries is silently discarded.

## Likelihood Explanation
The path is reachable via the public, unprivileged `submit_delivery_receipt` extrinsic — only `ensure_signed(origin)?` is required, with no relayer allowlist. The attacker (any relayer) does not need to forge anything; they only need a legitimately provable Ethereum event/receipt proof showing `success: false`, which is an expected, ordinary occurrence whenever a Gateway-side dispatch reverts. This makes the issue realistically and repeatably triggerable, not reliant on a compromised relayer or forged proof.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: if `true`, proceed with the existing reward payment and order removal; if `false`, withhold (or reduce) the reward and handle the failed-order lifecycle distinctly (e.g., emit a `MessageExecutionFailed` event, and decide whether to retain, retry, or remove the order with appropriate refund/compensation semantics) instead of treating failed execution identically to success.

## Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder` with `fee > 0` at a given `nonce`: [6](#0-5) .
2. The message is relayed to the Ethereum Gateway but the dispatched command reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer captures this genuine event/receipt proof and calls `submit_delivery_receipt(origin, event)`: [7](#0-6) .
4. `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` checks only `gateway` and the pending-order lookup by `nonce`; since `order.fee > 0`, it calls `register_reward` and removes the order regardless of `success`: [8](#0-7) .
6. Result: the relayer is rewarded for a message that failed to execute on Ethereum, and the order is closed out as if delivery succeeded — reproducible as a unit test asserting `register_reward` is called and `PendingOrders::get(nonce)` returns `None` even when constructing a `DeliveryReceipt` with `success: false`.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L41-50)
```rust
		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

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
