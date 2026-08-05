Audit Report

## Title
`success` field of Ethereum `DeliveryReceipt` is decoded but never checked before paying relayer reward and settling the order - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`Pallet::process_delivery_receipt` in the Snowbridge V2 outbound queue pays the relayer's `order.fee` and unconditionally removes the corresponding `PendingOrder` for any validly-proven `DeliveryReceipt`, without ever inspecting the `receipt.success` field that records whether the destination call on Ethereum actually succeeded. [1](#0-0)  The decoded `DeliveryReceipt::success` value, sourced from the Gateway's `InboundMessageDispatched` event, is fully populated during decode but never consulted in settlement logic. [2](#0-1) [3](#0-2) 

## Finding Description
`submit_delivery_receipt` is a public, unprivileged extrinsic reachable by any signed account. [4](#0-3)  It verifies the Merkle/receipt proof via `T::Verifier::verify`, decodes the event log into a `DeliveryReceipt` (which includes `success: bool`), and forwards the decoded receipt to `process_delivery_receipt`. Inside `process_delivery_receipt`, only `receipt.gateway` (address check) and `receipt.nonce` (map lookup) are used: `receipt.success` and `receipt.topic` are never read.

```rust
ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
...
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
Self::deposit_event(Event::MessageDelivered { nonce });
``` [5](#0-4) 

Because a message dispatch that reverts on Ethereum still produces a real, verifiable `InboundMessageDispatched(nonce, topic, success=false, reward_address)` log (this is normal Gateway contract behavior for reverted destination calls, not a forged event), a relayer can submit this genuinely-proven receipt through `submit_delivery_receipt`. The verifier accepts it since the proof is authentic, and `DeliveryReceipt::try_from` correctly decodes `success: false`. The settlement path then proceeds identically to the success case: full `order.fee` reward is registered and `PendingOrders` entry is permanently removed via `Event::MessageDelivered`, with no distinction from a genuinely successful dispatch.

## Impact Explanation
This violates the invariant that "bridge rewards ... and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A relayer is paid the full delivery fee and the pending order is permanently settled regardless of whether the Ethereum-side message execution actually succeeded. Since `PendingOrders::remove` is unconditional, a failed dispatch cannot be retried, refunded, or otherwise distinctly accounted for — the failure is silently discarded while payout occurs exactly as in the success case, causing unbacked/undifferentiated reward payout for failed bridge deliveries.

## Likelihood Explanation
The path is reachable by any relayer via the public `submit_delivery_receipt` extrinsic using a legitimate proof of a naturally-occurring reverted destination call (e.g., insufficient gas or downstream contract revert on Ethereum) — no forgery, governance, or privileged access is required. Such reverts are an expected, recurring occurrence in normal bridge operation, making this readily and repeatably triggerable.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: on `false`, skip or redirect reward payout and do not remove the `PendingOrder` via the same unconditional path as success — route it to a distinct failure-handling/refund flow and emit a distinguishable event (e.g., `MessageDispatchFailed`) instead of `MessageDelivered`. Add unit tests asserting no (or different) reward registration when `success: false` is submitted, mirroring the existing `submit_delivery_receipt_succeeds_after_unhalt` pattern. [6](#0-5) 

## Proof of Concept
1. A message with nonce `N` and non-zero `fee` is committed via `do_process_message`, inserting a `PendingOrder { nonce: N, fee, .. }`. [7](#0-6) 
2. The relayer relays the message to the Ethereum Gateway; the destination call reverts, so the Gateway emits `InboundMessageDispatched(nonce=N, topic, success=false, reward_address)`.
3. The relayer builds a genuine Merkle/receipt proof for this log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (real proof) and `DeliveryReceipt::try_from` decodes `success: false` correctly. [8](#0-7) 
5. `process_delivery_receipt` runs `T::RewardPayment::register_reward(&reward_account, .., order.fee)` and removes the order, identical to the success path, emitting `MessageDelivered`. [9](#0-8) 
6. Repeating with intentionally-failing target calls lets a relayer collect full delivery rewards for every dispatch, success or failure, with no distinguishing accounting.

### Citations

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
