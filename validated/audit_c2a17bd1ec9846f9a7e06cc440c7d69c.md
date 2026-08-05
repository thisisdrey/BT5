This confirms the claim's code citations are accurate and the `receipt.success` field is indeed decoded but never referenced in `process_delivery_receipt`. The reward is paid solely based on `order.fee > 0` and nonce lookup, with no branching on `success`.

Audit Report

## Title
Snowbridge outbound-queue-v2 pays relayer rewards regardless of the delivery-receipt's `success` flag - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

## Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries an explicit `success: bool` field indicating whether the message's commands actually executed on Ethereum [1](#0-0) , but `Pallet::process_delivery_receipt` never reads or checks this field before paying the relayer reward [2](#0-1) . This allows a relayer to collect the full fee for delivering a message whose on-chain dispatch failed.

## Finding Description
`submit_delivery_receipt` is a public, unprivileged extrinsic gated only by `ensure_signed` [3](#0-2) . It verifies the event log via `T::Verifier::verify` and decodes it into a `DeliveryReceipt`, which includes `success` from the Ethereum event `InboundMessageDispatched(nonce, topic, success, reward_address)` [4](#0-3) .

`process_delivery_receipt` then only checks `receipt.gateway` against `T::GatewayAddress`, looks up the `PendingOrder` by `receipt.nonce`, and pays the reward whenever `order.fee > 0`, unconditionally removing the order afterward [5](#0-4) . The decoded `receipt.success` value is never read anywhere in this function or in the wider pallet (confirmed by searching for `success` usages across the Snowbridge tree — none appear in `outbound-queue-v2`). The module-level documentation similarly only describes "pay reward with fee attached in the order" upon receipt with no mention of a success condition [6](#0-5) , confirming this is not merely a doc omission but a genuine gap in the payout logic — the field exists and is decoded but never consulted, exactly matching the described defect pattern (state captured but not read by the consuming gate).

## Impact Explanation
This is a duplicate/undue-settlement issue: reward funds (`T::RewardPayment::register_reward`) are credited to the relayer for every verified event log with a matching gateway and nonce, irrespective of whether the corresponding Ethereum dispatch actually succeeded. Since Ethereum-side command execution can legitimately revert (e.g., due to gas exhaustion or a command-level revert) independent of relayer honesty, a relayer is paid the same fee whether the message succeeded or failed, and the `PendingOrder` is removed either way with no path to reconcile or claw back the reward. This matches the "duplicate settlement or payout" / "theft of unbacked value" impact category in the accepted gate.

## Likelihood Explanation
No privileged actor, forged proof, or malicious collusion is required — any relayer submitting a legitimate `EventProof` for a real Ethereum transaction that emits `success: false` can collect the reward exactly as if the message had succeeded. `T::Verifier::verify` only attests to log inclusion/authenticity, not to the payment-eligibility semantics of the `success` bool, so this is reachable from ordinary, permissionless relayer activity.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before crediting `T::RewardPayment::register_reward` — pay in full (or a defined fraction) only when `success == true`, and either withhold, reduce, or route to a distinct accounting path when `success == false`. Add a regression test that submits a receipt with `success: false` and asserts no (or reduced) reward registration occurs.

## Proof of Concept
1. A message with `fee > 0` is queued via `do_process_message`, storing `PendingOrders[nonce] = PendingOrder { nonce, fee, block_number }` [7](#0-6) .
2. On Ethereum, the Gateway executes the message's commands, they revert, and the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer submits an `EventProof` for this real log via `submit_delivery_receipt`; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success = false` [8](#0-7) .
4. `process_delivery_receipt` finds `order.fee > 0`, calls `register_reward` unconditionally, and removes the `PendingOrder` — the relayer is paid in full despite the on-chain dispatch failure [9](#0-8) .

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
