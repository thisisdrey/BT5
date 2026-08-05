Confirmed: `success` is never referenced anywhere in the outbound-queue-v2 pallet logic besides being decoded once in `delivery_receipt.rs`. This validates the claim in full.

Audit Report

## Title
Outbound Queue V2 pays relayer reward and clears `PendingOrders` regardless of on-chain delivery `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_delivery_receipt` decodes a `DeliveryReceipt` whose `success: bool` field indicates whether the message actually executed successfully on Ethereum, but this field is never consulted before paying the relayer reward and removing the `PendingOrder`. As a result, a receipt for a *failed* delivery is settled identically to a successful one.

## Finding Description
`DeliveryReceipt` is decoded from the `InboundMessageDispatched` event log via `TryFrom<&Log>`, explicitly carrying `success` alongside `nonce`, `topic`, and `reward_address`: [1](#0-0) 

In `Pallet::process_delivery_receipt`, after verifying the gateway address and looking up `PendingOrders<T>` by `nonce`, the pallet unconditionally pays the reward (when `order.fee > 0`) and removes the pending order — `receipt.success` is never read: [2](#0-1) 

The only existing guard is `ensure!(T::GatewayAddress::get() == receipt.gateway, ...)`, which checks the emitting contract address but says nothing about delivery success. `submit_delivery_receipt` is a public extrinsic gated only by `ensure_signed(origin)?` and a valid Merkle/event-log proof via `T::Verifier::verify`, so any signed account holding a genuine proof for an `InboundMessageDispatched(success=false)` log can trigger full settlement: [3](#0-2) 

A grep across the outbound-queue-v2 pallet and the delivery-receipt primitive confirms `success` is decoded exactly once and never referenced again in any conditional, so no downstream code path branches on it.

## Impact Explanation
This is a duplicate/incorrect settlement bug: reward payout and `PendingOrder` removal (an irreversible state transition via `<PendingOrders<T>>::remove(nonce)`) advance for messages that did not actually execute successfully on Ethereum. This matches the "duplicate settlement or payout" and "runtime bugs that compromise intended behavior" categories in the impact gate — relayer-reward funds are paid out for undelivered/failed messages, and the corresponding nonce's accounting state is permanently destroyed, precluding any retry/refund accounting.

## Likelihood Explanation
High likelihood: `submit_delivery_receipt` requires only `ensure_signed(origin)` plus a valid proof for a real Ethereum log — it does not require a malicious relayer or privileged role, since genuine `success=false` events occur naturally whenever execution reverts on the Ethereum side (e.g., a reverted XCM leg). The bug is triggered on every ordinary failed-delivery receipt, not just crafted attacks.

## Recommendation
Branch on `receipt.success` inside `process_delivery_receipt`: only call `T::RewardPayment::register_reward` and remove the `PendingOrder` when `receipt.success == true`. For `success == false`, apply a distinct failure path (e.g., retain the order for retry, or settle it as a no-reward failure event) instead of silently paying and clearing state as if delivery succeeded.

## Proof of Concept
1. `do_process_message` queues a message with `nonce = N`, `fee > 0`, creating `PendingOrders[N]` [4](#0-3) .
2. The message is relayed to Ethereum but execution fails there, causing Ethereum to emit `InboundMessageDispatched(nonce=N, topic, success=false, reward_address=...)`.
3. Any signed account submits `submit_delivery_receipt` with a valid proof for that log; `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` decodes `success: false`.
4. `process_delivery_receipt` ignores `success`, pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward`, and removes `PendingOrders[N]` as if delivery succeeded [5](#0-4) .
5. A unit test can assert that after calling `process_delivery_receipt` with `receipt.success = false`, `PendingOrders::<T>::get(N)` becomes `None` and the reward ledger records a payout — demonstrating settlement without successful delivery.

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
