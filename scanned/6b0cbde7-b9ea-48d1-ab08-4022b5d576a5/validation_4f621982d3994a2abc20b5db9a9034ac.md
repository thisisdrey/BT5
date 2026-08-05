Confirmed: `DeliveryReceipt.success` is decoded from the Ethereum event but never consulted in `process_delivery_receipt`. The reward is paid solely based on `order.fee > 0`, with no check on whether `receipt.success` is `true`.### Title
`process_delivery_receipt` pays relayer rewards without checking the Ethereum-side `success` flag, allowing reward payout for failed message execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The external report's core broken invariant is: a value that must gate an action (a checkpoint keyed on the correct semantic domain) is fed the wrong signal, so the guard degenerates into "always pass" or "always fail," bypassing the intended check. The local analog: `DeliveryReceipt::success`, decoded straight from the Ethereum `InboundMessageDispatched` event, is never consulted before paying the relayer reward — the pallet only checks `order.fee > 0`, so a delivery receipt reporting execution failure (`success == false`) still results in a full reward payout.

### Finding Description
`bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs` decodes the Solidity event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` into a `DeliveryReceipt` struct that carries a `success: bool` field: [1](#0-0) 

`submit_delivery_receipt` in the `outbound-queue-v2` pallet verifies the Ethereum receipt proof, decodes the envelope into this `DeliveryReceipt`, and forwards it to `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` then looks up the `PendingOrder` by nonce and pays the reward whenever `order.fee > 0` — it never reads `receipt.success`: [3](#0-2) 

A `grep` across the entire pallet confirms `receipt.success` is never referenced anywhere in `outbound-queue-v2` — the field is decoded and then discarded. This is functionally the same class of bug as the report: a semantically meaningful input (timestamp vs. block number in the report; success vs. failure here) is dropped/mis-scoped in a threshold/gate check, so the guard no longer reflects the real-world condition it was meant to enforce.

### Impact Explanation
Any relayer can submit a syntactically valid, cryptographically verified delivery-receipt proof for a message whose execution on Ethereum reverted (`success = false`) and still collect the full reward attached to that order, because the only gate is `order.fee > 0`, unconditional on outcome. This is an unbacked/duplicate-style payout: the protocol pays for work it did not actually receive (successful message execution), draining relayer reward funds (`T::RewardPayment::register_reward`) for messages that never completed on the Ethereum side. This falls squarely under "theft or unbacked mint or unlock" / "duplicate settlement or payout" in the impact gate, since value is disbursed to the wrong condition (failed delivery) rather than being conserved and settled only on genuine success.

### Likelihood Explanation
This requires no privileged actor: `submit_delivery_receipt` is a plain signed extrinsic open to any relayer, and the only requirement is a real, verifiable Ethereum receipt proof for a `InboundMessageDispatched` event with `success = false` — which naturally occurs whenever gas estimation is wrong or the destination command reverts on Ethereum. No malicious peer, governance action, or leaked key is needed; a normal relayer submitting a receipt for a legitimately failed delivery triggers the erroneous payout under standard usage, matching the report's "occurs under normal usage" characterization.

### Recommendation
Gate the reward payment on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; for `success == false`, either withhold the reward entirely, or apply a distinct/reduced compensation path, while still removing the `PendingOrder` (or handling retry/requeue logic) to avoid leaving stale state. Emit a distinguishing event so failed deliveries are observable on-chain and reconciled correctly.

### Proof of Concept
Conceptual sequence, following the existing test harness pattern in `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs`:
1. A message is committed via `do_process_message`, creating `PendingOrder { nonce, fee: 1_000_000, .. }` as shown in `outbound_queue_v2::lib.rs` lines 426-436.
2. On Ethereum, the message execution reverts; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. The relayer builds a valid `EventProof` for this log (same proof-construction path used in existing tests, e.g. `mock_valid_event_proof` at `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` lines 374-388, but with `success` encoded as `false` in the event payload) and calls `submit_delivery_receipt`.
4. `T::Verifier::verify` succeeds (the proof is cryptographically valid), `DeliveryReceipt::try_from` decodes `success = false` correctly, but `process_delivery_receipt` still executes `T::RewardPayment::register_reward(&reward_account, .., order.fee)` because the code only checks `order.fee > 0`.
5. Result: the relayer collects the reward `order.fee` even though the message failed on Ethereum, confirmed by `assert_ok!` on `submit_delivery_receipt`/`process_delivery_receipt` and observing `Event::RewardRegistered` fire regardless of the `success` value — mirroring the existing `poc_m1` test structure at `bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs` lines 390-416, but toggling `success` instead of the halted-verifier flag to demonstrate the missing check.

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
