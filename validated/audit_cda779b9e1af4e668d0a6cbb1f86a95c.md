Confirmed: the `success` field is decoded but never referenced anywhere in `outbound-queue-v2/src/lib.rs` (zero matches for "success" in that pallet), and across the entire `bridges/snowbridge` tree the only place `.success` is read is the decode site in `delivery_receipt.rs` itself. `process_delivery_receipt` unconditionally pays the reward and clears `PendingOrders` regardless of the decoded `success` value.

Audit Report

## Title
`process_delivery_receipt` ignores the `DeliveryReceipt.success` flag, paying relayer rewards and settling orders even on failed Ethereum dispatch - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`DeliveryReceipt` decodes the `InboundMessageDispatched` Ethereum event, which carries an explicit `success: bool` field documented as "Delivery status" [1](#0-0) . `Pallet::process_delivery_receipt` never reads or branches on `receipt.success`; it unconditionally pays the relayer reward for `order.fee > 0` and removes the `PendingOrder` from storage [2](#0-1) .

## Finding Description
`submit_delivery_receipt` is callable by any signed account, verifies the proof via `T::Verifier::verify`, decodes the log into `DeliveryReceipt`, and forwards to `process_delivery_receipt` [3](#0-2) . Inside `process_delivery_receipt`, the only checks are gateway address match and existence of a `PendingOrder` for `receipt.nonce`; there is no `ensure!(receipt.success, ...)` or conditional branch — both success and failure paths pay the reward and remove the order, emitting `Event::MessageDelivered` unconditionally [4](#0-3) . The `success` field is faithfully decoded from the real Ethereum event data (`event.success`) [5](#0-4) , so it reflects the real on-chain Ethereum dispatch outcome, but the pallet discards it — confirmed by the fact that no reference to `.success`/`success` exists anywhere else in `outbound-queue-v2/src/lib.rs` or elsewhere in the `bridges/snowbridge` tree outside the decode site itself.

## Impact Explanation
If the message dispatch on Ethereum actually fails (`success = false`), the relayer still collects the fee and `PendingOrders[nonce]` is irreversibly removed, closing off any retry/re-settlement path for that nonce. This is a fund-flow correctness bug in a public, unprivileged, signed extrinsic that governs bridge reward payout, matching the "duplicate settlement or payout" / "permanent bridge-state lock" impact class in the gate: the pending order's terminal state is finalized without regard to the real Ethereum outcome, and reward is paid even though the underlying message effect that the reward is meant to compensate never completed.

## Likelihood Explanation
Any relayer observing a Gateway `InboundMessageDispatched` event with `success = false` can submit it through `submit_delivery_receipt` with a normal, genuine merkle/beacon proof — no privileged origin, governance, or malicious-peer assumption is required; this is the ordinary relayer workflow, applied to a normal/expected failure event that Ethereum contracts legitimately emit. This is fully repeatable for every failed dispatch.

## Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only invoke `T::RewardPayment::register_reward` when `success == true`; for `success == false`, skip the reward and either retain the order for a bounded retry window or transition it to a distinct terminal "failed" state/event, rather than unconditionally calling `<PendingOrders<T>>::remove(nonce)` and emitting `Event::MessageDelivered`.

## Proof of Concept
1. `do_process_message` queues a message, inserting `PendingOrders[nonce]` with `fee > 0` [6](#0-5) .
2. The message is relayed to Ethereum, and dispatch on the Gateway fails, producing `InboundMessageDispatched { success: false, ... }`.
3. Any signed relayer submits this genuine event log + proof via `submit_delivery_receipt`; `T::Verifier::verify` succeeds (real proof), and `DeliveryReceipt::try_from` decodes `success: false` faithfully [7](#0-6) .
4. `process_delivery_receipt` performs its gateway check and nonce lookup, finds `order.fee > 0`, calls `T::RewardPayment::register_reward`, and removes the order — identical to the success path — because `receipt.success` is never inspected [8](#0-7) .

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
