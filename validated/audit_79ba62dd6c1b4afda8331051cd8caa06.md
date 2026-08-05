This confirms `receipt.success` is decoded from the Ethereum event log but referenced only in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` at its struct definition — it is never read anywhere else in the codebase, including `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`.Audit Report

## Title
`process_delivery_receipt` ignores the `DeliveryReceipt.success` flag and unconditionally pays the relayer reward for failed Ethereum-side message execution - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` containing a `success: bool` field that reflects whether the corresponding command actually executed successfully on the Ethereum Gateway contract, but the function never reads or checks this field before paying the relayer reward via `T::RewardPayment::register_reward` and permanently removing the `PendingOrder`. [1](#0-0)  The module-level documentation itself confirms this is the intended (flawed) design: it describes the flow as simply "Fetch the pending order by nonce of the message, pay reward with fee attached in the order" with no mention of checking execution success. [2](#0-1) 

## Finding Description
`submit_delivery_receipt` is a public, unprivileged, signed extrinsic that verifies an Ethereum event log/proof and decodes it into a `DeliveryReceipt`. [3](#0-2)  `DeliveryReceipt` is decoded from the `InboundMessageDispatched` Solidity event, which explicitly carries a `success` flag describing whether the dispatched command executed successfully or reverted on the Gateway. [4](#0-3)  The `TryFrom<&Log>` implementation faithfully decodes this flag into `DeliveryReceipt.success`. [5](#0-4) 

`process_delivery_receipt` only checks `receipt.gateway == T::GatewayAddress::get()` and that a `PendingOrder` exists for `receipt.nonce`; `receipt.success` is decoded but never referenced anywhere else in the codebase (confirmed via repository-wide search — the only occurrence of the `success` field is its definition). It unconditionally calls `T::RewardPayment::register_reward` when `order.fee > 0` and then calls `<PendingOrders<T>>::remove(nonce)`, permanently closing the order regardless of execution outcome. [6](#0-5)  The `PendingOrder` struct persisted in storage also carries no completion/success semantics, so there is no way to reconcile a failed execution after the fact. This violates the required invariant that receipts and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically.

## Impact Explanation
This allows relayer rewards on BridgeHub to be paid and orders to be permanently closed (`PendingOrders::remove`) for messages that reverted/failed to execute on Ethereum, decoupling reward payout from delivered value — a duplicate/unbacked settlement of funds to the wrong condition (i.e., payout despite `success = false`). This matches the "duplicate settlement or payout" impact category from the gate, since the pallet settles and irreversibly finalizes a payout that should have been gated on `receipt.success`.

## Likelihood Explanation
Likelihood is Medium: any ordinary relayer submitting a genuine, correctly-verified proof of an `InboundMessageDispatched` event with `success = false` (e.g., due to insufficient dispatch gas relative to `GasMeter::maximum_dispatch_gas_used_at_most`, or any other legitimate execution-time revert on the Gateway) triggers full reward payment with no privileged actor, malicious peer, or compromised prover required.

## Recommendation
1. Add an explicit check (e.g., `ensure!(receipt.success, Error::<T>::DeliveryFailed)`) or a separate failure-handling branch in `process_delivery_receipt` before calling `T::RewardPayment::register_reward`.
2. Separate order resolution from reward payment: only pay the relayer when `receipt.success == true`; on failure, emit a distinct event and handle fee refund/retry logic explicitly.
3. Persist enough context in `PendingOrder` (e.g., `topic`) to reconcile receipts against the exact committed message and cross-check `receipt.topic` against the nonce's original message identity.

## Proof of Concept
1. `do_process_message` enqueues a message and creates `PendingOrders[nonce] = { nonce, fee, block_number }` with non-zero `fee`. [7](#0-6) 
2. The message is relayed to Ethereum; the dispatched command reverts on the Gateway (e.g., insufficient dispatch gas). The Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid proof for this genuine event and calls `submit_delivery_receipt(event)`; `T::Verifier::verify` and `DeliveryReceipt::try_from` succeed, decoding `success = false` correctly.
4. `process_delivery_receipt` checks only `receipt.gateway` and `PendingOrders::get(nonce).is_some()`; both pass, `order.fee > 0` triggers `T::RewardPayment::register_reward`, and `PendingOrders::remove(nonce)` closes the order permanently — despite `success == false`.
5. The relayer collects the full fee for a message that never executed successfully on Ethereum, with no mechanism to reconcile or reverse the payout.

### Citations

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
