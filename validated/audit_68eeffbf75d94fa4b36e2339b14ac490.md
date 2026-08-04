Confirmed: `process_delivery_receipt` in `snowbridge-pallet-outbound-queue-v2` never inspects the `success` field of the decoded `DeliveryReceipt`. It just fetches the `PendingOrder` by `nonce` and unconditionally pays the fee whenever `order.fee > 0`, regardless of whether the Ethereum-side event indicates the message actually executed successfully. This mirrors the report's bug class ("wrong/missing input actually used to determine the correct value to act on") — here it's a missing/unused field (`success`) rather than a wrong ID, but the effect is the same class of data-validation gap: the payout decision is made without validating the field that should gate it.

### Title
Relayer reward paid regardless of `DeliveryReceipt.success`, decoupling payout from actual delivery outcome - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`process_delivery_receipt` decodes a `DeliveryReceipt` (which carries a `success: bool` field describing whether the message dispatch on Ethereum succeeded) but never reads or checks that field before paying out the relayer's fee from `PendingOrders`.

### Finding Description
The delivery receipt type explicitly carries a `success` flag: [1](#0-0) 

But `process_delivery_receipt` only checks the gateway address and the existence of a `PendingOrder` for the `nonce`, then pays the reward if `order.fee > 0` — `receipt.success` is decoded but never inspected: [2](#0-1) 

Since `receipt.nonce`, `receipt.gateway`, and `receipt.reward_address` are attacker/relayer-controlled event-log fields decoded from a real (but arbitrary-content) Ethereum event log verified only for provenance from the correct Gateway contract, any relayer can submit a delivery receipt for a message whose on-chain Ethereum execution actually failed (`success = false`) and still collect the full relayer fee, exactly as if the message had succeeded.

### Impact Explanation
This is a public, underpriced-work class issue: the pallet is meant to reward relayers for successfully delivering messages to Ethereum, but the reward is paid purely based on nonce existence in `PendingOrders`, not on the delivery outcome. A relayer can be rewarded for work that did not actually complete, draining the fee/reward pool without providing the guaranteed service (successful delivery), and the "unlock/completion" semantics of `MessageDelivered` are emitted even though nothing was actually delivered.

### Likelihood Explanation
Likelihood is high and requires no privileged access: any unprivileged relayer account can call `submit_delivery_receipt` with a genuine-but-failed transaction's event log (a `success = false` `InboundMessageDispatched` event is a legitimate, unmodified event that Ethereum's Gateway contract can emit), since the extrinsic is open to any signed origin and the light-client proof only attests that the log was emitted, not that the underlying operation succeeded.

### Recommendation
- **Short term:** Add `ensure!(receipt.success, Error::<T>::DeliveryFailed)` (or an equivalent gating check) in `process_delivery_receipt` before paying out `order.fee`, so failed deliveries do not consume the pending fee (they could instead be settled with no reward, refunded, or retried).
- **Long term:** Add unit/integration tests asserting that a `DeliveryReceipt` with `success: false` results in no `RewardRegistered`/`register_reward` call, and audit other decoded-but-unused fields in receipt/verification structures across Snowbridge V2 pallets for the same class of "decoded but not validated" gaps.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { fee: F, .. }`. [3](#0-2) 
2. On Ethereum, message dispatch fails and the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer submits this real event log + valid proof via `submit_delivery_receipt`. `T::Verifier::verify` succeeds (the log genuinely exists and is provable); `DeliveryReceipt::try_from` decodes `success = false` correctly. [4](#0-3) 
4. `process_delivery_receipt` proceeds: gateway matches, `PendingOrders::get(nonce)` returns `Some(order)`, `order.fee > 0`, so `T::RewardPayment::register_reward` is called and the order is removed — despite `receipt.success == false`. [5](#0-4) 

Note: I could not find any existing test in this pallet asserting reward-denial on `success: false` (existing tests such as `poc_m1` only cover the halted-verifier case), which is consistent with this gap being unaddressed. If the runtime team has since added a `success` check elsewhere (e.g. in `T::RewardPayment` implementations not visible in this index slice), that would need to be verified in a full checkout, since the index may not include every downstream `RewardPayment` implementation.

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
