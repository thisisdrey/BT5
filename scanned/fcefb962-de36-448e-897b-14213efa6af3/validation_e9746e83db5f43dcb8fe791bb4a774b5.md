Based on my investigation, I found a concrete analog: the `success` field of the Ethereum `InboundMessageDispatched` delivery receipt is decoded and carried through `DeliveryReceipt`, but it is never checked before paying out the relayer reward in `process_delivery_receipt`.

### Title
Relayer reward paid regardless of delivery outcome (`success` field ignored) - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The Ethereum `InboundMessageDispatched` event explicitly carries a `success: bool` field indicating whether the relayed message was actually executed successfully on the destination side [1](#0-0) . This value is decoded into `DeliveryReceipt::success` [2](#0-1) , but `Pallet::process_delivery_receipt` never inspects `receipt.success` before paying the reward — it only checks `order.fee > 0` and pays out unconditionally [3](#0-2) .

### Finding Description
`submit_delivery_receipt` verifies the Ethereum proof (header/receipt inclusion) and decodes the log into a `DeliveryReceipt`, then calls `process_delivery_receipt` [4](#0-3) . Inside `process_delivery_receipt`, the only checks performed are: gateway address match, and whether a `PendingOrder` exists for the nonce; the fee is paid to `reward_account` whenever `order.fee > 0`, with no branch on `receipt.success` [5](#0-4) . This mirrors the audited bug class in the report: an output/side-value that is part of the proven structure (the pairing check in the report; here the `success` boolean in the verified log) is decoded but not enforced by the verifying validator logic, silently accepting an outcome that should be rejected or treated differently.

### Impact Explanation
Because the reward is registered purely based on the existence of a `PendingOrder` and a verified log matching that nonce — irrespective of whether the destination-side dispatch actually succeeded — a relayer can be rewarded for delivering a message that failed on Ethereum. This decouples payout from the intended "successful delivery" invariant and can result in unbacked/incorrect reward issuance from `T::RewardPayment::register_reward`, which is a value-accounting integrity problem for the bridge reward system.

### Likelihood Explanation
This does not require a malicious relayer, validator, or governance action beyond normal operation: any relayer who submits a legitimately verifiable `InboundMessageDispatched(success=false, ...)` event (which occurs naturally whenever destination dispatch fails, e.g., due to insufficient weight/funds on the Ethereum-XCM execution side) will still receive full payment. This is a systemic logic gap rather than a crafted forgery, so it is highly likely to trigger under normal failure conditions rather than only under adversarial proof forgery.

### Recommendation
- Short term: In `process_delivery_receipt`, branch on `receipt.success`; only pay the relayer reward when `success == true`, and handle the `false` case explicitly (e.g., emit a `MessageDeliveryFailed` event, still remove/resolve the `PendingOrder` without payment, or apply a different, reduced settlement).
- Long term: Audit all decoded-but-unused fields (`success`, `topic`, etc.) across Snowbridge receipt/message decoders to ensure every semantically meaningful field from a verified log is enforced in validator/payout logic, and add property/fuzz tests that submit receipts with `success=false` to confirm no reward is paid.

### Proof of Concept
1. A `PendingOrder { nonce, fee, .. }` exists in storage from a prior `send_message` call.
2. On Ethereum, the destination dispatch fails, and the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer builds a valid `EventProof` for this real, legitimately included log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (real, valid inclusion proof); `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` checks only `T::GatewayAddress::get() == receipt.gateway` and `order.fee > 0`, then calls `T::RewardPayment::register_reward(...)` and removes the `PendingOrder` — despite `success == false` — as shown at [6](#0-5) .

Note: I was unable to verify within the available search results whether any downstream/relayer-off-chain component or a different validation layer (outside this pallet) additionally checks `success` before a relayer would bother submitting a failed receipt — this is a purely on-chain logic gap that I could confirm from the pallet code itself.

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
