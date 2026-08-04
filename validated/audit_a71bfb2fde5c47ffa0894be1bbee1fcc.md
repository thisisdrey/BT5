### Title
`process_delivery_receipt` ignores `receipt.success` and pays relayer reward for failed Ethereum executions - (`File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The Snowbridge outbound queue v2 pallet pays the relayer reward and removes the `PendingOrder` whenever a delivery receipt is verified, regardless of whether the Ethereum execution succeeded. The `DeliveryReceipt` struct includes a `success` boolean decoded from the `InboundMessageDispatched` event, but `process_delivery_receipt` never checks it. This is the local analog of the external bug: a single required invariant (here, "only pay rewards for successfully executed messages") is not enforced because the code omits a conjunction.

### Finding Description
In `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`, `submit_delivery_receipt` verifies the event proof and decodes a `DeliveryReceipt` from the event log, then calls `process_delivery_receipt`. That function:

1. Checks `receipt.gateway` matches `T::GatewayAddress`.
2. Computes the reward account from `receipt.reward_address` or the relayer.
3. Loads the `PendingOrder` by `receipt.nonce`.
4. If `order.fee > 0`, calls `T::RewardPayment::register_reward`.
5. Removes the pending order and emits `MessageDelivered`.

At no point does it inspect `receipt.success`. The `DeliveryReceipt` struct in `bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` does decode `event.success` from the Ethereum `InboundMessageDispatched` event, so the value is available.

### Impact Explanation
A relayer can submit a valid, verified delivery receipt for a message that reverted or failed on Ethereum (`success == false`) and still receive the full `order.fee`. This violates the intended bridge accounting invariant that rewards are only paid for successful deliveries. It is an unauthorized payout / unbacked reward: the relayer is compensated even though no successful cross-chain work was performed. Because `submit_delivery_receipt` is a public extrinsic, any signed origin can trigger this once a valid proof exists.

### Likelihood Explanation
Likely. The `success` field is part of the verified event log, so the data is trustworthy and available at zero extra cost. The only missing step is the guard. A relayer (or any caller) has a direct financial incentive to submit receipts for failed executions because the fee is paid out anyway. The verifier only proves that the event was emitted by the Gateway contract; it does not interpret `success`, so the runtime must do so.

### Recommendation
Add an `ensure!(receipt.success, Error::<T>::DeliveryFailed);` check at the start of `process_delivery_receipt`, before computing the reward account or loading the order. Alternatively, if failed deliveries should still remove the pending order to prevent replay, split the logic: require `success` to pay the fee, but always remove the order. The minimal fix is:

```rust
ensure!(receipt.success, Error::<T>::DeliveryFailed);
```

Add a corresponding `DeliveryFailed` variant to `Error<T>` and regression tests covering both `success == false` (no payout, order removed or not depending on design) and `success == true` (normal payout).

### Proof of Concept
1. A user sends an outbound message; `do_process_message` creates `PendingOrder { nonce: 1, fee: 1_000_000, ... }`.
2. The message is relayed to Ethereum but execution reverts; the Gateway emits `InboundMessageDispatched(nonce=1, topic=..., success=false, reward_address=...)`.
3. A relayer builds a valid event proof for that log and calls `submit_delivery_receipt(origin, proof)`.
4. `T::Verifier::verify` passes because the log is genuinely emitted by the Gateway.
5. `process_delivery_receipt` loads `PendingOrders[1]`, sees `fee > 0`, and calls `register_reward(&reward_account, kind, 1_000_000)`.
6. The relayer receives 1_000_000 even though `receipt.success` is `false`.

### Supporting Citations
- `DeliveryReceipt` decodes `success` but it is never read in the pallet: [1](#0-0) 
- `submit_delivery_receipt` verifies proof, decodes receipt, and dispatches to `process_delivery_receipt`: [2](#0-1) 
- `process_delivery_receipt` pays reward and removes order without checking `success`: [3](#0-2)

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
