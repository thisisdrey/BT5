Confirmed: `process_delivery_receipt` never inspects `receipt.success` at all. [1](#0-0) 

Audit Report

## Title
`process_delivery_receipt` pays relayer reward and clears `PendingOrder` without ever checking the decoded `receipt.success` flag from the Ethereum `InboundMessageDispatched` event - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`DeliveryReceipt` decodes a `success: bool` field from the Ethereum `InboundMessageDispatched` event log, indicating whether the message dispatch actually succeeded on Ethereum. [2](#0-1)  `submit_delivery_receipt` verifies the proof and decodes the receipt, then unconditionally calls `process_delivery_receipt`, which pays the relayer reward and removes the `PendingOrder` based only on `order.fee > 0` — `receipt.success` is never read or branched on anywhere in the function body. [3](#0-2) 

## Finding Description
`process_delivery_receipt` checks the gateway address, resolves the reward account, fetches the `PendingOrder` by nonce, and pays out `order.fee` via `T::RewardPayment::register_reward` whenever `order.fee > 0`, then removes the order and emits `MessageDelivered` — at no point does it inspect `receipt.success`. [4](#0-3)  The `Error` enum has no variant for a failed on-chain dispatch, only `InvalidGateway`, `InvalidPendingNonce`, `RewardPaymentFailed`, etc. [5](#0-4)  This confirms the field is decoded purely for record-keeping/reward-address extraction and is otherwise discarded, so a genuine Ethereum-side dispatch failure (`success: false`) is treated identically to a successful dispatch.

## Impact Explanation
This is a duplicate-settlement/unbacked-payout class issue: the relayer reward (`order.fee`) is paid via `T::RewardPayment::register_reward` and the `PendingOrder` is permanently removed even when the underlying Ethereum dispatch failed. This both incorrectly disburses value from the reward pool for non-completed work and permanently destroys the retry/accounting state (`PendingOrders`) for that nonce, since the order is unconditionally removed regardless of `success`.

## Likelihood Explanation
`submit_delivery_receipt` only requires `ensure_signed` — any relayer holding a real, verifiable Ethereum event log (including a genuine `success: false` dispatch-failure event emitted by the actual Gateway contract) can trigger this path with no special privilege. [6](#0-5)  No collusion, governance action, or compromised infrastructure is needed — an honest relayer relaying a real failed-dispatch event is sufficient.

## Recommendation
In `process_delivery_receipt`, branch explicitly on `receipt.success`: only call `T::RewardPayment::register_reward` when `success == true`; when `false`, either leave the `PendingOrder` for retry/re-attribution or route to a distinct failure-settlement path (e.g., a new `Event::MessageDeliveryFailed` and appropriate bookkeeping) instead of paying the reward and silently removing the order.

## Proof of Concept
1. A message is committed via `do_process_message`, inserting a `PendingOrder{nonce, fee, block_number}` into `PendingOrders`. [7](#0-6) 
2. On Ethereum, the Gateway dispatches the command but it fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits this genuine event via `submit_delivery_receipt`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false`. [8](#0-7) 
4. `process_delivery_receipt` proceeds past the gateway check, pays `order.fee` to the reward account via `register_reward`, and removes the `PendingOrder`, exactly as it would for `success: true`. [9](#0-8) 
5. A unit test mirroring `submit_delivery_receipt_succeeds_after_unhalt` but with `success: false` in the mock event would confirm identical reward payout and order removal, demonstrating the missing check.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L225-243)
```rust
	#[pallet::error]
	pub enum Error<T> {
		/// The message is too large
		MessageTooLarge,
		/// The pallet is halted
		Halted,
		/// Invalid Channel
		InvalidChannel,
		/// Invalid Envelope
		InvalidEnvelope,
		/// Message verification error
		Verification(VerificationError),
		/// Invalid Gateway
		InvalidGateway,
		/// Pending nonce does not exist
		InvalidPendingNonce,
		/// Reward payment failed
		RewardPaymentFailed,
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
