The claim is confirmed by direct inspection of the code. `DeliveryReceipt` decodes a `success: bool` field from the Ethereum `InboundMessageDispatched` event [1](#0-0) , and `process_delivery_receipt` never reads `receipt.success` — it unconditionally pays the reward when `order.fee > 0` and removes the `PendingOrder` regardless of delivery outcome [2](#0-1) . The extrinsic `submit_delivery_receipt` is a public, unprivileged entry point requiring only `ensure_signed` and a valid verifier proof, with no additional restriction tied to success [3](#0-2) .

Audit Report

## Title
Relayer reward paid and pending order settled without checking Ethereum execution success flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`process_delivery_receipt` fetches the `PendingOrder` by `nonce`, unconditionally pays `order.fee` to the relayer via `T::RewardPayment::register_reward`, and removes the order from `PendingOrders` storage — but it never inspects `receipt.success`, the field decoded directly from the Ethereum `InboundMessageDispatched` event that indicates whether the delivered command actually executed successfully on the Gateway contract.

## Finding Description
`DeliveryReceipt` carries a `success: bool` field sourced from the on-chain Ethereum event `InboundMessageDispatched(nonce, topic, success, reward_address)` [1](#0-0) . In `submit_delivery_receipt`, the extrinsic verifies the proof, decodes the receipt, and calls `process_delivery_receipt` [3](#0-2) . Inside `process_delivery_receipt`, the code checks the gateway address, looks up the `PendingOrder` by `nonce`, pays `order.fee` if it is greater than zero, and unconditionally removes the entry — `receipt.success` is never read [4](#0-3) . This settles the pending state and pays the relayer regardless of whether the underlying message actually executed successfully on Ethereum.

## Impact Explanation
This breaks the intended settlement invariant that the reward compensates a relayer strictly for successful delivery. If a command fails on the Ethereum side (`success: false`, e.g. a revert due to gas exhaustion or a Gateway-side precondition failure), the relayer still collects the full fee, and the `PendingOrder` record is deleted as though delivery succeeded, losing any signal for retry/monitoring logic. This is an unbacked/duplicate-style payout on public underpriced work and a value-conservation violation for bridge rewards, matching the "theft or unbacked mint or unlock" / "public underpriced work" impact categories.

## Likelihood Explanation
Any signed account can call `submit_delivery_receipt` with a genuine, verifiable event log/proof for a message whose Ethereum-side execution failed — no privileged relayer, validator, or governance role is required. This is a public, unprivileged entry point reachable by anyone holding the (public) log and proof data.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `success == true`. When `success == false`, still remove/settle the `PendingOrder` (to avoid it being stuck forever) but skip reward payment, and emit a distinct event (e.g. `MessageDeliveryFailed`) so downstream consumers can distinguish successful settlement from failed execution.

## Proof of Concept
1. A message with `fee > 0` is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, block_number }` [5](#0-4) .
2. On Ethereum, the corresponding command reverts/fails, causing the Gateway to emit `InboundMessageDispatched(nonce, topic, success: false, reward_address)`.
3. Anyone with this log and a valid proof calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false` correctly, but `process_delivery_receipt` ignores it: `order.fee > 0` still triggers `register_reward`, and `<PendingOrders<T>>::remove(nonce)` clears state as if delivery succeeded [6](#0-5) .
5. The caller collects the full reward for a failed delivery, and the record of the failed command is lost from on-chain state.

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
