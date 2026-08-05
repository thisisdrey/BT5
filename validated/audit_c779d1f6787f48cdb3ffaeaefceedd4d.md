Confirmed by direct inspection of the current source.

Audit Report

## Title
`process_delivery_receipt` ignores the decoded `success` flag, paying relayer rewards and settling pending orders for failed Ethereum message dispatches - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
The Snowbridge outbound queue v2 pallet decodes a `DeliveryReceipt` from an Ethereum `InboundMessageDispatched` log that carries an authenticated `success: bool` field, but `Pallet::process_delivery_receipt` never reads or branches on `receipt.success` before paying the relayer's fee via `T::RewardPayment::register_reward` and removing the `PendingOrders` entry. This allows relayers to collect rewards and permanently close bookkeeping for messages whose execution on Ethereum actually failed.

## Finding Description
`DeliveryReceipt::try_from` decodes the `success` field straight from the proven Ethereum log [1](#0-0) . The extrinsic `submit_delivery_receipt` verifies the log via `T::Verifier::verify`, decodes the receipt, and forwards it to `process_delivery_receipt` [2](#0-1) . Inside `process_delivery_receipt`, the gateway address is checked, the `PendingOrder` is fetched by nonce, and — provided `order.fee > 0` — `T::RewardPayment::register_reward` is called unconditionally, followed by an unconditional `<PendingOrders<T>>::remove(nonce)` and `Event::MessageDelivered` emission [3](#0-2) . At no point in this function, or anywhere else in the pallet (confirmed via search — no other reference to `success` exists in this crate), is `receipt.success` inspected. `PendingOrder` is created with `fee` and `block_number` only, and there is no field to retain nonce/fee state for a failed-dispatch retry path [4](#0-3) .

## Impact Explanation
This violates the settlement invariant that payout and queue-marker removal must only advance after execution succeeds. A relayer submitting a legitimately proven receipt for a message whose on-chain Ethereum dispatch reverted (`success == false`) still triggers full reward payout when `order.fee > 0`, and the `PendingOrders` entry is deleted regardless, permanently discarding the only state tying that nonce to its fee/retry tracking. This matches the required impact of duplicate/incorrect settlement and payout theft from the reward/treasury source, and permanent loss of bridge-state tracking for the affected nonce.

## Likelihood Explanation
No privileged action or compromise is required — an honest relayer processing an ordinary, expected outcome (`success=false` is a normal branch of the event schema, e.g. due to a reverting `Command` or insufficient `GasMeter` allocation on Ethereum) will trigger this unconditionally, since the pallet performs no success check at all. This is a deterministic code-path bug, not a probabilistic or attacker-effort-dependent exploit, making it highly likely to occur during normal bridge operation whenever any Ethereum-side dispatch fails.

## Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`. For `success == false`, avoid deleting the `PendingOrder` unconditionally — either preserve it for retry/resend, or transition it to a distinct failure-handling path — and emit a distinguishing event instead of unconditionally emitting `MessageDelivered`.

## Proof of Concept
1. A message is enqueued via `do_process_message`, inserting `PendingOrders[nonce]` with `fee > 0` [5](#0-4) .
2. On Ethereum, the Gateway's dispatch of the inbound command fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid proof for that log and calls `submit_delivery_receipt(origin, event)`; `T::Verifier::verify` succeeds since the log is authentic, and `DeliveryReceipt::try_from` decodes `success: false`.
4. `process_delivery_receipt` executes: because `receipt.success` is never read, `order.fee > 0` still triggers `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, and `<PendingOrders<T>>::remove(nonce)` runs unconditionally [6](#0-5) .
5. Result: the relayer is paid for a message that never executed successfully on Ethereum, and the nonce's tracking/order state is permanently gone, with no retry path.

### Citations

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
