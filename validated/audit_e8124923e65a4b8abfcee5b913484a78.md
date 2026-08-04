## Finding: Ignored `success` field in Snowbridge outbound-queue-v2 delivery-receipt settlement

I looked at several candidate analogs (message-queue processing, inbound-queue-v2 nonce handling, outbound-queue-v2 delivery receipts). The strongest local match to the report's core defect — *using stale/invalid state without checking a validity flag before it drives a settlement outcome* — is in the Snowbridge outbound-queue-v2 pallet, where the `success` field of a verified `DeliveryReceipt` is decoded but never consulted before the pallet pays the relayer reward and finalizes (removes) the pending order.

### Title
Relayer reward and order settlement in `process_delivery_receipt` ignore the `success` flag of the delivery receipt - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` explicitly carries a `success: bool` field describing whether the outbound command actually executed successfully on Ethereum [1](#0-0) . `Pallet::process_delivery_receipt` decodes this receipt, verifies the proof, looks up the `PendingOrder` by nonce, pays the relayer reward from `order.fee`, and removes the order — all without ever reading `receipt.success` [2](#0-1) . A grep across the pallet confirms `success` is decoded once in the primitives crate and never read anywhere else in the pallet logic.

### Finding Description
The doc comment for the module states the intended flow: after a message is "verified and executed" on Ethereum, the relayer submits the receipt to "pay reward with fee attached in the order" and remove the order [3](#0-2) . This implies settlement should only finalize once the message's *execution* on Ethereum is confirmed successful — mirroring the pivot requirement that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

In practice, `process_delivery_receipt` only checks the `gateway` address and the existence of a `PendingOrder` for the nonce:

```rust
ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
...
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
``` [4](#0-3) 

Because `receipt.success` is not checked, any relayer who obtains (or replays) a verified `InboundMessageDispatched` log where `success == false` — i.e., the Gateway contract itself reported that the dispatched command reverted on Ethereum — still receives the full relayer reward, and the `PendingOrder` is unconditionally removed and marked `MessageDelivered`. There is no distinct failure path, no re-queuing, and no differentiation in the emitted event. The chain-side settlement state (`PendingOrders` removal + `MessageDelivered` event + reward payout) therefore advances identically whether or not the corresponding cross-chain action actually completed.

### Impact Explanation
This breaks the intended one-to-one binding between "message executed successfully on the destination chain" and "reward paid / order settled." A command that fails on Ethereum (e.g., a transfer or XCM `Transact` that reverts) is nonetheless treated by BridgeHub as fully and successfully delivered: the relayer is paid, and no other component of the runtime is informed that the corresponding cross-chain effect (e.g., an asset unlock or agent transact) never took place. This can misalign the two sides of the bridge state — funds may be considered "sent" on BridgeHub bookkeeping even though the Ethereum-side effect did not occur — and it removes any on-chain signal that would allow retry/compensation logic to fire for failed commands.

### Likelihood Explanation
Triggering this requires nothing more than an unprivileged, honest relaying of a legitimate Ethereum event log with `success = false` — no malicious relayer, validator, or governance action is needed, since `success` is emitted by the (trusted) Gateway contract itself whenever gas runs out or a command genuinely reverts on execution. The only actor needed is anyone permissionlessly calling `submit_delivery_receipt`, which is unauthenticated except for `ensure_signed` [5](#0-4) .

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only pay the reward and remove the `PendingOrder` (emit `MessageDelivered`) when `success == true`; on `success == false`, emit a distinct failure event (e.g. `MessageDispatchFailed`) and decide an explicit policy for the order (e.g. still remove it to avoid a permanent lock, but without reward, or route it into a compensation/retry path) so that settlement state only advances in lock-step with confirmed execution outcome.

### Proof of Concept
1. A message is enqueued and processed by `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0` [6](#0-5) .
2. The message is relayed to Ethereum, and its dispatch reverts; the Gateway contract still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains an execution/receipt proof for this log and calls `submit_delivery_receipt` [5](#0-4) .
4. `process_delivery_receipt` verifies gateway and nonce only, pays the full `order.fee` reward, removes `PendingOrders[nonce]`, and emits `MessageDelivered` — identical to the success case, despite the underlying command having failed on Ethereum [2](#0-1) .

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
