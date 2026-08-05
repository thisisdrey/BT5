I found a concrete, exploitable local analog. Let me verify there's no other on-chain check of the `success` field anywhere in the pallet or reward path.Confirmed — the `success` field of `DeliveryReceipt` is never referenced anywhere in `bridges/snowbridge/pallets/outbound-queue-v2/`, so there is no code path that gates reward payment or order removal on whether the Ethereum-side dispatch actually succeeded.

### Title
`process_delivery_receipt` pays relayer reward and finalizes order regardless of Ethereum dispatch outcome, ignoring `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The `InboundMessageDispatched` Solidity event emitted on the Ethereum Gateway carries a `success` boolean indicating whether the message's commands actually executed successfully on Ethereum. This is decoded into `DeliveryReceipt.success` on the BridgeHub side, but `Pallet::process_delivery_receipt` never inspects this field: it unconditionally pays the relayer's fee via `T::RewardPayment::register_reward`, removes the `PendingOrder`, and emits `Event::MessageDelivered` for both successful and failed dispatches. This mirrors the reported `Receipt` event bug class — a single receipt/event path carries two semantically distinct outcomes (successful payment vs. a different, non-payment case) but is treated identically by the consuming code.

### Finding Description
The `DeliveryReceipt` struct decoded from the Ethereum event log explicitly tracks delivery status: [1](#0-0) 

`submit_delivery_receipt` verifies the proof, decodes the receipt, and forwards it to `process_delivery_receipt` without any success gating: [2](#0-1) 

`process_delivery_receipt` itself only checks the gateway address and the presence of a `PendingOrder` for the nonce — it never reads `receipt.success` before paying the reward, deleting the order, and emitting `MessageDelivered`: [3](#0-2) 

A grep across the pallet confirms `success` is never referenced anywhere in this crate, so no other guard exists. The invariant "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" is violated: the on-chain state (`PendingOrders` removal + `MessageDelivered` + relayer reward) advances identically whether the Ethereum-side execution succeeded or reverted.

### Impact Explanation
Any relayer can submit a valid, correctly-proved event log for a message whose Ethereum-side commands failed (`success: false`) — this is a legitimate outcome that can occur non-maliciously (e.g., insufficient gas allotted by `GasMeter`, a reverting command, transient Ethereum-side failure) and requires no malicious peer, validator, or governance actor. The relayer still collects the full `order.fee` reward as if the message had been successfully delivered, and the `PendingOrder` is permanently removed, foreclosing any retry/resend mechanism for the failed message. This is a public, unprivileged-entrypoint bug that causes incorrect reward payout (fee paid despite failed work) and permanent loss of the ability to track/retry the failed message — both align with the "duplicate settlement or payout" / "public underpriced work" / "permanent... bridge-state lock" impact classes.

### Likelihood Explanation
High likelihood: `submit_delivery_receipt` is a public, signed, unprivileged extrinsic; failure receipts (`success: false`) are a normal, expected outcome path in the documented Ethereum-execution pipeline (the module doc even states delivery is checked "When the message has been verified and executed"), so the failing branch is reachable without any adversarial setup — a relayer merely needs to relay the genuine failure event log which Ethereum will emit.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`. For `success == false`, emit a distinct event (e.g., `Event::MessageDeliveryFailed { nonce }`) and decide whether to retain the `PendingOrder` (to allow a resend/retry mechanism) or handle it via an explicit failure-settlement path, rather than silently paying the reward and discarding order state as if delivery succeeded.

### Proof of Concept
1. A message is enqueued and processed by `do_process_message`, creating `PendingOrders[nonce]` with `fee > 0`.
2. On Ethereum, the Gateway processes the message but one of its commands reverts, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any account (no special privilege) calls `submit_delivery_receipt` with a valid Merkle/verifier proof of this real (unmodified) event log.
4. `T::Verifier::verify` succeeds (proof is genuine), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` runs: gateway check passes, `PendingOrders::get(nonce)` returns `Some(order)`, `order.fee > 0` so `T::RewardPayment::register_reward` pays the relayer the full fee, `PendingOrders::remove(nonce)` deletes the order, and `Event::MessageDelivered { nonce }` is emitted — identical to the successful-delivery path, even though `receipt.success == false`. [4](#0-3)

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
