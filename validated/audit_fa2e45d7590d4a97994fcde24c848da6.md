## Title
Relayer reward paid on `submit_delivery_receipt` regardless of `DeliveryReceipt.success` value - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The `WeightedFarmingPool` report's core broken invariant is: state-changing accounting operations (weight/reward-rate updates) execute and settle without verifying that the caller/condition is legitimate. The local analog is in Snowbridge's outbound queue v2 delivery-receipt flow: `Pallet::process_delivery_receipt` pays out the relayer reward and clears the `PendingOrders` entry for a nonce without ever checking the `success` field decoded from the Ethereum `InboundMessageDispatched` event, meaning a receipt reporting execution failure still triggers full reward settlement exactly as if delivery had succeeded.

### Finding Description
`submit_delivery_receipt` decodes an `EventProof` from Ethereum, verifies the merkle/consensus proof via `T::Verifier::verify`, and converts the log into a `DeliveryReceipt` struct that explicitly carries a `success: bool` field [1](#0-0) , sourced from the on-chain event `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` [2](#0-1) .

After proof verification, the extrinsic calls `Self::process_delivery_receipt(relayer, receipt)`: [3](#0-2) 

`process_delivery_receipt` only validates the `gateway` address and that the `nonce` has a matching `PendingOrders` entry — it never inspects `receipt.success` before paying the reward and removing the pending order: [4](#0-3) 

Because `success` is decoded but discarded, both a genuine successful dispatch and a genuine failed dispatch on the Ethereum side produce byte-identical treatment on the Substrate side: the relayer reward (`order.fee`) is unconditionally paid via `T::RewardPayment::register_reward`, and the `PendingOrders` entry is unconditionally removed, permanently closing out the order regardless of whether the message was actually executed. This violates the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here settlement advances even when the report says execution did not succeed.

### Impact Explanation
This allows relayer rewards to be paid for messages whose execution on Ethereum failed, and it permanently retires the `PendingOrders` record for that nonce (no retry/second-chance path is visible in this function), which can be leveraged to drain reward funds disproportionate to actual delivered work and to silently and irrecoverably close out failed orders — a form of incorrect/duplicate-style settlement outcome, and public underpriced/unbacked payout of protocol funds without commensurate service delivered. Impact is scoped to relayer-fee accounting for Snowbridge V2 delivery receipts, not full chain takeover, but it directly matches the "theft or unbacked mint," "duplicate settlement or payout," and "reward payout without atomic success" impact classes in scope.

### Likelihood Explanation
The path requires only a signed account submitting a valid consensus/verifier proof for a real Ethereum log — no privileged origin, governance, or malicious-validator assumption is needed (`ensure_signed(origin)` is the only origin check) [5](#0-4) . Any relayer that legitimately attempts delivery and the message execution reverts on Ethereum (e.g., due to gas exhaustion or downstream failure, which is exactly why `success` exists as a field) can submit that receipt and still collect the fee, since nothing in `process_delivery_receipt` gates on it.

### Recommendation
In `process_delivery_receipt` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`), branch on `receipt.success` before paying the reward and/or removing the `PendingOrders` entry: on failure, either withhold/reduce the reward, keep the order open for a future correct receipt, or emit a distinct `MessageDeliveryFailed` event instead of `MessageDelivered`, ensuring reward settlement only occurs atomically with confirmed successful execution as the report's remediation pattern (checking real state before allowing settlement) prescribes.

### Proof of Concept
1. Relayer submits a Snowbridge V2 outbound message; `PendingOrders` stores `{nonce, fee, block_number}` in `do_process_message` [6](#0-5) .
2. Message execution on the Ethereum Gateway reverts/fails; the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Relayer captures this log/proof and calls `submit_delivery_receipt(origin, event)` on BridgeHub.
4. `T::Verifier::verify` succeeds (the log genuinely exists and is included in the block), `DeliveryReceipt::try_from` decodes `success=false` successfully.
5. `process_delivery_receipt` checks only `gateway` and looks up `PendingOrders::get(nonce)` — both pass — then unconditionally calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and removes the pending order, paying the relayer as if the message had been executed successfully, even though `receipt.success == false`. [7](#0-6)

### Citations

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L10-12)
```rust
sol! {
	event InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address);
}
```

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
