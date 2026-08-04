## Finding

The `success` field decoded from Ethereum's `InboundMessageDispatched` event is completely ignored by `process_delivery_receipt`, so a relayer reward is paid out **regardless of whether the message actually executed successfully on Ethereum**. This is the direct structural analog of the BunniSwap bug: the pallet grants a nonzero "output" (relayer reward payout) without requiring the corresponding "input" (a successful delivery) to be true.

### Title
Relayer reward paid regardless of delivery outcome due to unchecked `success` field - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decodes a `success: bool` field from the `InboundMessageDispatched` Ethereum event log [1](#0-0) , but `Pallet::process_delivery_receipt` never reads or checks this field before paying out the relayer's reward [2](#0-1) .

### Finding Description
`submit_delivery_receipt` verifies the Merkle/header proof and decodes the event log into a `DeliveryReceipt` struct, then calls `process_delivery_receipt` [3](#0-2) . That function checks the gateway address, looks up the `PendingOrder` by `nonce`, and unconditionally pays `order.fee` to the reward account if `order.fee > 0`, then removes the pending order:

```rust
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
``` [4](#0-3) 

`receipt.success` is never inspected. The `InboundMessageDispatched` event on Ethereum emits `success=false` when the corresponding command execution on Ethereum reverted/failed [5](#0-4) . Because this pallet does not gate the reward on `success == true`, a relayer can submit a valid proof for a delivery attempt that failed on Ethereum (e.g., an out-of-gas or reverted command execution) and still collect the full relayer reward as if delivery had succeeded — an unpriveleged, unprivileged-relayer-reachable path (`submit_delivery_receipt` is a plain signed extrinsic) that produces value (a reward payout) without the corresponding real-world work being completed. Existing guards (`GatewayAddress` check, `Verifier::verify` proof check, `InvalidPendingNonce`) only validate that *some* real event log for that nonce exists — none of them validate the outcome recorded in that same event log.

### Impact Explanation
This breaks the intended one-for-one binding between "successful message delivery" and "reward payout" described in the pallet's own module documentation ("pay reward with fee attached in the order" only after delivery is verified) [6](#0-5) . It allows relayers to be rewarded for failed/no-op deliveries, draining the bridge's reward pool without the associated cross-chain work being performed — a form of underpriced/unbacked payout matching the "public underpriced work" and "duplicate/incorrect settlement" impact categories for bridge processing.

### Likelihood Explanation
Likelihood is high: any signed relayer account can call `submit_delivery_receipt` [3](#0-2)  with a genuine event log/proof for a message whose execution on Ethereum reverted (`success=false`), which is a normal occurrence (e.g., a command that runs out of gas), and still collect the reward — no malicious relayer/validator collusion or privileged access is required, only a legitimately failed but provable delivery.

### Recommendation
- **Short term**: In `process_delivery_receipt`, require `ensure!(receipt.success, Error::<T>::DeliveryFailed)` (or otherwise branch reward logic) before calling `T::RewardPayment::register_reward`, so failed deliveries do not earn a reward (or earn a reduced/partial reward per protocol design).
- **Long term**: Add invariant/property tests (in the spirit of the BunniSwap report's Medusa suggestion) asserting that `RegisteredRewardAmount`/`RegisteredRewardsCount` never increase when the decoded `DeliveryReceipt.success == false`, across all delivery-receipt code paths.

### Proof of Concept
1. A message is queued and gets a `PendingOrder { nonce, fee, .. }` via `do_process_message` [7](#0-6) .
2. The relayer executes the corresponding command on Ethereum, but it reverts (e.g., insufficient gas provided at the max-gas ceiling), so the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer builds a legitimate Merkle/EventProof for this real event log and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (it's a real, correctly proven log) [8](#0-7) , `DeliveryReceipt::try_from` decodes `success: false` correctly [9](#0-8) .
5. `process_delivery_receipt` ignores `receipt.success`, finds `PendingOrders[nonce]`, and pays `order.fee` to the relayer, removing the order [4](#0-3) .
6. The relayer has been paid in full for a delivery that failed on Ethereum, and cannot be charged again for the same nonce since the order is now removed.

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
