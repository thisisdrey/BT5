### Title
`success` field of Ethereum `DeliveryReceipt` is decoded but never checked before paying relayer reward and settling the order - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The Snowbridge V2 outbound queue pays out the relayer reward and permanently settles a `PendingOrder` whenever `submit_delivery_receipt` is called with a validly-proven Ethereum event log, regardless of whether the dispatched message on Ethereum actually succeeded. This mirrors the reported `treasury.fc` bug class exactly: a message that signals an error/failure condition (here, `success: false` in the `InboundMessageDispatched` event) is processed identically to a success message, so error responses are not distinguished from success responses before performing payout/accounting.

### Finding Description
The Ethereum Gateway contract emits `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` whenever a message is dispatched, whether the destination call succeeded or reverted [1](#0-0) . This event is decoded into a `DeliveryReceipt` struct that explicitly carries a `success: bool` field labeled "Delivery status" [2](#0-1) .

However, `Pallet::process_delivery_receipt` in the outbound-queue-v2 pallet never reads or checks `receipt.success` anywhere in its logic:

```rust
pub fn process_delivery_receipt(
    relayer: <T as frame_system::Config>::AccountId,
    receipt: DeliveryReceipt,
) -> DispatchResult
{
    ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
    let reward_account = if receipt.reward_address == [0u8; 32] { relayer } else { receipt.reward_address.into() };
    let nonce = receipt.nonce;
    let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
    if order.fee > 0 {
        T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
    }
    <PendingOrders<T>>::remove(nonce);
    Self::deposit_event(Event::MessageDelivered { nonce });
    Ok(())
}
``` [3](#0-2) 

Only `receipt.gateway` (address check) and `receipt.nonce` (lookup) are used; `receipt.success` and `receipt.topic` are entirely unused. Regardless of the success flag from Ethereum, the full `order.fee` is always paid out via `T::RewardPayment::register_reward`, and the `PendingOrder` is unconditionally removed from `PendingOrders`, permanently marking the message as delivered via the `MessageDelivered` event. A `grep` over the entire `outbound-queue-v2` pallet confirms `success` is never referenced outside the struct definition.

This is architecturally identical to the reported bug: in `treasury.fc`, an `ok?`-style error indicator was unused and error responses were processed with the same accounting path as success responses (full reward/slash calculation applied) instead of being short-circuited. Here, the `success` indicator from the delivery receipt is likewise unused, and failure responses are processed with the exact same reward/settlement path as success responses.

### Impact Explanation
Because the reward is paid and the order permanently removed irrespective of `success`, there is no differentiated accounting between "message successfully executed on Ethereum" and "message dispatch reverted/failed on Ethereum." This can lead to:
- Relayers being fully rewarded for message deliveries whose payload execution failed on the Ethereum side, if that is not the intended reward model.
- No possibility of re-queueing, refunding, or otherwise accounting for a failed message once `PendingOrders` entry is removed — the failure is silently discarded (only an event is fired) with the same payout outcome as success.
- Downstream systems or higher-privileged actors relying on the fee/reward-vs-success invariant (e.g., budget/accounting parity between Ethereum execution outcome and Polkadot-side settlement) can accumulate silent accounting drift over time, analogous to the treasury contract's stake mis-accounting.

This falls under the "Balances, assets, ... bridge rewards, and contract-held value must conserve value and settle exactly once to the rightful beneficiary and amount" and "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivots, since settlement (`MessageDelivered`, reward registration, order removal) proceeds even when execution has explicitly failed per the on-chain proof itself.

### Likelihood Explanation
The path is reachable by any relayer via the public, unprivileged `submit_delivery_receipt` extrinsic [4](#0-3) . No governance, admin, or privileged actor is required — a relayer only needs a valid Merkle/receipt proof for *any* dispatched message (success or failure) from the known Gateway, which occurs naturally whenever an Ethereum-side call reverts. This is a normal, expected occurrence (e.g., insufficient gas, downstream contract revert) rather than a contrived edge case, so the likelihood of this state being reached during ordinary bridge operation is high.

### Recommendation
- Explicitly branch on `receipt.success` in `process_delivery_receipt`.
- On `success == false`, skip (or reduce/redirect) the reward payout — do not treat it identically to the success path — and/or route the pending order into a distinct failure-handling/refund flow instead of silently removing it with a `MessageDelivered` event.
- Ensure the `MessageDelivered` event (or a new `MessageDispatchFailed` event) accurately reflects the on-chain outcome so downstream consumers/indexers can distinguish successful settlement from failed dispatch.
- Add unit tests asserting no reward registration (or a different reward amount / no removal without proper accounting) when `success: false` is submitted, mirroring the existing `submit_delivery_receipt_succeeds_after_unhalt` test pattern [5](#0-4) .

### Proof of Concept
1. A message with nonce `N` and non-zero `fee` is queued and committed by the outbound queue (`do_process_message`), creating a `PendingOrder { nonce: N, fee, .. }` [6](#0-5) .
2. A relayer relays the message to the Ethereum Gateway; the destination call reverts, so the Gateway emits `InboundMessageDispatched(nonce=N, topic, success=false, reward_address)`.
3. The relayer builds a Merkle/receipt proof for this event log (a legitimate log — no forgery needed) and calls `submit_delivery_receipt` on Bridge Hub.
4. `T::Verifier::verify` succeeds (it is a real, valid proof), and `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` is invoked: it checks `gateway` and finds the `PendingOrders` entry, then unconditionally executes `T::RewardPayment::register_reward(&reward_account, .., order.fee)` and removes the order — exactly as if `success` had been `true`.
6. The relayer receives full reward and the order is marked `MessageDelivered`, even though the message payload execution on Ethereum failed. Repeating this with intentionally-failing target calls lets a relayer collect full delivery rewards for every dispatch, success or failure, with no distinguishing accounting.

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-12)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L420-449)
```rust
#[test]
fn submit_delivery_receipt_succeeds_after_unhalt() {
	new_tester().execute_with(|| {
		let nonce = 0;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_valid_event_proof());

		// Bridge halted — receipt rejected, order untouched.
		set_verifier_halted(true);
		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);
		assert!(PendingOrders::<Test>::get(nonce).is_some());

		// Bridge resumed — same receipt succeeds and the order is settled.
		set_verifier_halted(false);
		assert_ok!(OutboundQueue::submit_delivery_receipt(origin, event));
		assert!(PendingOrders::<Test>::get(nonce).is_none());

		System::assert_has_event(mock::RuntimeEvent::OutboundQueue(Event::MessageDelivered {
			nonce,
		}));
	});
}
```
