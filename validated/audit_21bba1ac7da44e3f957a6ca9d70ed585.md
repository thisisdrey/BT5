This confirms the claim exactly as described. The code at `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` never inspects `receipt.success` — it only checks `receipt.gateway`, resolves `nonce`, and unconditionally pays the reward and removes the order if `order.fee > 0`.This confirms `success` is decoded into `DeliveryReceipt` but never referenced anywhere else in the codebase outside of the decode site — no other consumer checks it before or after `process_delivery_receipt`. The `success` field is dead data as far as reward/settlement logic is concerned.

Audit Report

## Title
Reward payout on delivery receipt ignores dispatch `success` flag, causing incorrect settlement regardless of actual message execution outcome - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet decodes a `DeliveryReceipt` containing a `success: bool` field that reflects whether the message dispatch on Ethereum actually succeeded, but the function never reads or branches on this field before paying the relayer reward and removing the `PendingOrder`. Any relayer who submits a valid proof for a message whose Ethereum-side dispatch failed (`success = false`) still receives the full fee reward and the order is settled as if delivery succeeded.

## Finding Description
The `DeliveryReceipt` struct decoded from the `InboundMessageDispatched` Ethereum event log carries `success: bool`, populated directly from the event data in `TryFrom<&Log> for DeliveryReceipt`: [1](#0-0) [2](#0-1) 

The public, unprivileged extrinsic `submit_delivery_receipt` verifies the Merkle/Beefy proof of the event log, decodes it into a `DeliveryReceipt`, and forwards it to `process_delivery_receipt`: [3](#0-2) 

`process_delivery_receipt` only checks `receipt.gateway` against the known gateway address, resolves `reward_account`, looks up `PendingOrders` by `nonce`, and — if `order.fee > 0` — unconditionally calls `T::RewardPayment::register_reward`, then removes the order and emits `MessageDelivered`, without ever inspecting `receipt.success`: [4](#0-3) 

A repo-wide search confirms `success` is set only at decode time and is never read anywhere else in the pallet or the wider bridge codebase, so no other code path compensates for this omission — the field is effectively dead data. The module-level documentation states the intended design as "When the message has been verified and executed, the relayer will call `submit_delivery_receipt`... to fetch the pending order... pay reward" [5](#0-4) , implying reward should be conditioned on successful execution, which the implementation fails to enforce.

## Impact Explanation
This falls under "duplicate settlement or payout" / incorrect settlement of bridge reward state, since `PendingOrder` removal and reward payment (via `T::RewardPayment::register_reward` for `order.fee`) occur identically whether the destination-chain dispatch succeeded or reverted. The invariant broken is: bridge payout state must only advance to a paid/settled state after the underlying dispatch is confirmed successful — here it advances unconditionally on any verified delivery event, correct or failed. This causes an economically incorrect reward for relayers of failed messages and misrepresents the on-chain event (`MessageDelivered` is emitted regardless of actual delivery success).

## Likelihood Explanation
No malicious behavior or privileged access is required. `submit_delivery_receipt` is a public, signed extrinsic callable by any relayer [6](#0-5) , and `receipt.success = false` arises naturally whenever a legitimate command execution reverts on Ethereum (e.g., insufficient gas budget, application-level revert). Any relayer submitting a real, valid proof for such a failed dispatch will trigger the mispayout deterministically and repeatably — this is not a rare edge case but an expected occurrence in bridge operation whenever dispatches fail.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`, proceed with the existing reward payment and order removal.
- If `false`, remove/settle the `PendingOrder` (to prevent permanent lock of that slot) but skip `T::RewardPayment::register_reward`, and emit a distinct event (e.g., `MessageDispatchFailed`) rather than `MessageDelivered`, so failed dispatches are neither rewarded nor conflated with successful delivery.

## Proof of Concept
1. `do_process_message` queues a message and creates `PendingOrders[nonce]` with `fee > 0` [7](#0-6) .
2. The relayer relays the message to Ethereum; the destination command reverts/fails during execution, causing the Gateway contract to emit `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer submits `submit_delivery_receipt` with a valid Merkle/Beefy proof of this real event log.
4. `T::Verifier::verify` succeeds (genuine event); `DeliveryReceipt::try_from` decodes `success = false` correctly [2](#0-1) .
5. `process_delivery_receipt` ignores `success`, sees `order.fee > 0`, calls `register_reward`, removes the order, and emits `MessageDelivered` — paying the relayer for a failed dispatch [8](#0-7) .

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
