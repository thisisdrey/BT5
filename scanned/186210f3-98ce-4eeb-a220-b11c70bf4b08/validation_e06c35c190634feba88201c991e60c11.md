## Analysis

Confirmed: `process_delivery_receipt` in `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs` decodes a `DeliveryReceipt` (which carries a `success: bool` field indicating whether the message actually executed successfully on the Ethereum Gateway) but never inspects `receipt.success` before paying the relayer reward and clearing the `PendingOrder`. [1](#0-0) [2](#0-1) 

### Title
Relayer reward paid and pending order settled without checking Ethereum execution success flag — (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`process_delivery_receipt` fetches the `PendingOrder` by `nonce`, unconditionally pays `order.fee` to the relayer via `T::RewardPayment::register_reward`, and removes the order — but it never checks `receipt.success`, the field decoded straight from the Ethereum `InboundMessageDispatched` event log that indicates whether the command actually executed successfully on the Gateway contract.

### Finding Description
The `DeliveryReceipt` type explicitly carries a `success: bool` field sourced from the on-chain Ethereum event `InboundMessageDispatched(nonce, topic, success, reward_address)` [1](#0-0) . This field is the on-chain signal of whether the delivered command actually completed successfully when executed on Ethereum.

In `submit_delivery_receipt`, the extrinsic verifies the proof and decodes the receipt, then calls `process_delivery_receipt` [3](#0-2) . Inside `process_delivery_receipt`, the code only checks the gateway address, looks up the `PendingOrder` by nonce, and if `order.fee > 0` pays the reward, then unconditionally removes the entry from `PendingOrders`: [4](#0-3) 

`receipt.success` is never read anywhere in this function. This mirrors the reported bug-class exactly: an operation (paying a reward / settling pending state) is finalized without properly accounting for the fact that the underlying action did not complete as expected — analogous to the Kryptonite report where a validator entry was removed from the registry while the corresponding redelegation (the "real" follow-up action) was silently dropped instead of being tracked for retry. Here, the `PendingOrder` (the tracked pending work item) is cleared and the reward paid out regardless of whether the message was actually executed successfully on Ethereum.

### Impact Explanation
This breaks the intended incentive/settlement invariant that the outbound queue module documents in its own header comment (`b. Fetch the pending order by nonce of the message, pay reward with fee attached in the order`) — the reward is meant to compensate a relayer for successful delivery of the message. If a command fails on the Ethereum side (`success: false`, e.g. it reverted due to gas exhaustion, a malformed payload, or an unmet precondition on the Gateway), the relayer still collects the full fee, and the `PendingOrder` is deleted as if delivery had succeeded. Any retry/monitoring logic built on `PendingOrders` (e.g. off-chain relayers watching for orders still pending re-delivery) loses the record that the command failed and needs to be resent — effectively "inadequate tracking of a pending action" once the underlying attempt is dropped from storage, exactly the class of issue from the report. This causes underpriced/unbacked reward payout, a value-conservation violation for bridge rewards.

### Likelihood Explanation
Any account can submit a `submit_delivery_receipt` extrinsic with a valid event proof for a message whose on-chain execution failed on Ethereum (e.g. a command that reverts due to gas limit or destination-side error) as long as the event log itself is genuine and verifiable — no privileged relayer, validator, or governance action is required. This is a public, unprivileged entry point.

### Recommendation
Check `receipt.success` in `process_delivery_receipt`: only pay the relayer's reward when `success == true`. When `success == false`, still remove/settle the `PendingOrder` (to avoid it staying stuck forever) but do not pay a reward, and emit a distinct event (e.g. `MessageDeliveryFailed`) so that consumers of `PendingOrders` state can distinguish successful settlement from failed execution and potentially re-queue or account for it.

### Proof of Concept
1. A message with `fee > 0` is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, block_number }`.
2. On Ethereum, the corresponding command reverts/fails when the relayer submits it to the Gateway, causing the Gateway to emit `InboundMessageDispatched(nonce, topic, success: false, reward_address)`.
3. The relayer (or anyone with the log+proof) calls `submit_delivery_receipt` with this event log and a valid proof.
4. `T::Verifier::verify` succeeds (the event genuinely happened), `DeliveryReceipt::try_from` decodes `success: false` correctly, but `process_delivery_receipt` ignores it: `order.fee > 0` still triggers `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, and `<PendingOrders<T>>::remove(nonce)` clears the pending state as if it were a genuine successful delivery [5](#0-4) .
5. The relayer collects the full reward for a failed delivery, and the record that the corresponding command actually failed on Ethereum is lost from on-chain state.

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
