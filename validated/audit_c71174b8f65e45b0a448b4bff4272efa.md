The code confirms the claim exactly as described. `process_delivery_receipt` reads `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce`, but never reads `receipt.success` before unconditionally calling `T::RewardPayment::register_reward` when `order.fee > 0`. [1](#0-0) [2](#0-1) 

Audit Report

## Title
`process_delivery_receipt` ignores the `success` flag of `DeliveryReceipt`, rewarding relayers for failed message deliveries - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

## Summary
`DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries a `success: bool` field indicating whether the Ethereum-side message execution actually succeeded [3](#0-2) . `Pallet::process_delivery_receipt` verifies the gateway address, looks up the pending order by nonce, and unconditionally pays the relayer reward whenever `order.fee > 0`, without ever reading `receipt.success` [4](#0-3) .

## Finding Description
The extrinsic `submit_delivery_receipt` verifies the log proof via `T::Verifier::verify`, decodes the event into a `DeliveryReceipt`, and calls `process_delivery_receipt` [5](#0-4) . Inside `process_delivery_receipt`, only `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce` are consumed; `receipt.success` is never referenced [1](#0-0) . `T::Verifier::verify` only proves the log's authenticity/inclusion; it makes no assertion about the semantic `success` flag inside the decoded event data, and `InvalidPendingNonce` only checks order existence, not delivery outcome. Since `DeliveryReceipt::try_from` faithfully decodes `success` from the ABI event without loss [6](#0-5) , this is a pure business-logic omission rather than a decode issue: a genuine, honestly-proven event where Ethereum-side dispatch reverted (`success = false`) still reaches the unconditional reward payment.

## Impact Explanation
Any signed account can submit a delivery receipt for a message whose Ethereum-side execution failed and still collect the full relayer reward tied to `PendingOrder.fee`, exactly as if delivery succeeded. Because `<PendingOrders<T>>::remove(nonce)` runs regardless of outcome, this permanently closes the order, precluding any later correct accounting for that nonce. This constitutes theft/unbacked payout from the reward pot triggered by a permissionless caller acting on public, genuine on-chain data — matching the "theft or unbacked mint or unlock" / "duplicate settlement or payout" impact category.

## Likelihood Explanation
High. No malicious relayer, prover, or privileged role is required — a failed Ethereum-side dispatch (e.g., destination contract revert or out-of-gas) is a normal, permissionless occurrence, and any account can call the public `submit_delivery_receipt` extrinsic with the genuine event and proof. The vulnerability is a deterministic logic gap, not a probabilistic or adversarial-proof-forgery condition.

## Recommendation
In `process_delivery_receipt`, branch explicitly on `receipt.success`: only invoke `T::RewardPayment::register_reward` when `receipt.success == true`. For `success == false`, still remove/consume the `PendingOrder` (or route to a distinct failure/refund path) but must not credit the relayer reward, and emit a distinct event (e.g. `MessageDeliveryFailed`) rather than `MessageDelivered` so downstream consumers are not misled.

## Proof of Concept
1. `do_process_message` creates a `PendingOrder { nonce, fee > 0, .. }` for an outbound message [7](#0-6) .
2. On Ethereum, the Gateway attempts to dispatch the inbound message, execution fails, and the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any account collects the log and inclusion proof and calls `submit_delivery_receipt(origin, event)` [5](#0-4) .
4. `T::Verifier::verify` succeeds and `DeliveryReceipt::try_from` correctly decodes `success = false`, but `process_delivery_receipt` still executes `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and emits `MessageDelivered` [8](#0-7) .
5. The caller is rewarded for a message that never completed successfully on Ethereum; a unit test constructing a `DeliveryReceipt` with `success: false` and asserting `register_reward` is *not* called would fail against current code, confirming the defect.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L38-51)
```rust
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
