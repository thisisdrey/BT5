Confirmed: `DeliveryReceipt.success` is decoded from the Ethereum `InboundMessageDispatched` event but is never checked in `process_delivery_receipt`. The relayer reward is paid based solely on `order.fee > 0` and the gateway/nonce match, regardless of whether `success` is `true` or `false`. [1](#0-0) [2](#0-1) 

### Title
Relayer reward paid regardless of on-chain message execution outcome, ignoring `DeliveryReceipt.success` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge outbound-queue-v2 pallet's `process_delivery_receipt` pays out the relayer reward for any nonce with a matching `PendingOrder` and correct gateway address, without inspecting the `success` field of the `DeliveryReceipt` that is decoded from the Ethereum `InboundMessageDispatched` event log.

### Finding Description
`submit_delivery_receipt` verifies the event log/proof via `T::Verifier::verify`, decodes it into a `DeliveryReceipt` (which contains a `success: bool` field taken directly from the emitted Ethereum event), and calls `process_delivery_receipt`. [3](#0-2) 

`process_delivery_receipt` checks only that `receipt.gateway` matches `T::GatewayAddress`, looks up the `PendingOrder` by `receipt.nonce`, and — if `order.fee > 0` — unconditionally calls `T::RewardPayment::register_reward` for the reward account, then removes the order and emits `MessageDelivered`. [2](#0-1) 

The `success` field decoded from the Ethereum log is never read anywhere in this payment path — grepping the pallet confirms no reference to `receipt.success` in `process_delivery_receipt` or `submit_delivery_receipt`. This means the on-chain gateway can legitimately emit `InboundMessageDispatched(nonce, topic, success=false, reward_address)` for a command that reverted or failed to execute on Ethereum, and the pallet will still treat this as a completed, rewardable delivery.

This breaks the "settle exactly once after execution succeeds" invariant expected for message queues / delivery-receipt proofs: the receipt proof is valid (real event, real gateway, real nonce), so `T::Verifier::verify` and the gateway/nonce checks all pass — but the corrupted invariant is that **reward settlement is not conditioned on successful execution of the outbound command**, only on the message having been *dispatched* (attempted) on Ethereum.

### Impact Explanation
Any relayer that submits a valid delivery-receipt proof for a *failed* execution (`success=false`) on Ethereum still collects the full `order.fee` reward meant to compensate for successful relay-and-execution. This is a public, unprivileged, underpriced-work-style issue: normal operation of the gateway contract (which legitimately emits `success=false` for reverted commands, e.g. due to insufficient gas, bad command data, or downstream contract reverts) directly triggers reward payout with no execution guarantee, draining the reward pool for work that did not achieve its intended effect. No malicious relayer, validator, or governance actor is required — only a real, unmodified Ethereum gateway emitting a normal failure event.

### Likelihood Explanation
High. `success=false` is an expected outcome path of the Ethereum-side `InboundMessageDispatched` event (not a forged/adversarial condition), so this triggers under ordinary bridge operation whenever a command fails on Ethereum for any reason (gas, reverted call, bad payload). Any relayer who relays such a message and submits the proof through the normal, unprivileged `submit_delivery_receipt` extrinsic receives the reward.

### Recommendation
In `process_delivery_receipt`, condition the reward payment (and potentially the removal of the pending order / semantics of `MessageDelivered`) on `receipt.success`. Only call `T::RewardPayment::register_reward` when `receipt.success == true`; on failure, either withhold reward, apply a different (e.g., partial gas-only) reward, or route to a distinct failure-handling path/event so that fund distribution matches actual instruction execution outcome.

### Proof of Concept
1. A message with `fee = 1_000_000` is enqueued and processed via `do_process_message`, creating `PendingOrder { nonce, fee, .. }` [4](#0-3) .
2. The Gateway contract on Ethereum attempts the command; it reverts/fails and legitimately emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` (this is the contract's own expected failure-signalling behavior).
3. Any relayer submits this genuine event + proof via `submit_delivery_receipt(origin, event)`. `T::Verifier::verify` succeeds because the proof is real; `DeliveryReceipt::try_from` decodes `success=false` correctly [5](#0-4) .
4. `process_delivery_receipt` only checks `gateway` and looks up `PendingOrders`, ignoring `receipt.success`, and unconditionally pays out `order.fee` to the relayer/reward account, then removes the order [6](#0-5) .
5. Result: the relayer is rewarded for a message whose Ethereum-side execution failed, with no code path recovering or withholding the fee.

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
