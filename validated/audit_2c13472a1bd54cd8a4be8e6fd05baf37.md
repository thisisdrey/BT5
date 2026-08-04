### Title
`process_delivery_receipt` pays relayer reward without checking the decoded `success` flag of the delivery receipt - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
The external report's core broken invariant is: a caller-supplied/derived expiry/validity flag exists but is never checked before the state-changing action (payout) proceeds. The local analog is in Snowbridge's `snowbridge-pallet-outbound-queue-v2`: the `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event log carries a `success: bool` field that records whether the message actually executed successfully on Ethereum, but `Pallet::process_delivery_receipt` never inspects it before paying out the relayer reward and clearing the pending order.

### Finding Description
`DeliveryReceipt` is decoded from an on-chain Ethereum event log and explicitly carries a `success` field: [1](#0-0) 

The `submit_delivery_receipt` extrinsic verifies the receipt's Merkle/event proof via `T::Verifier::verify`, decodes it into a `DeliveryReceipt`, and forwards it to `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` then validates the gateway address and the pending-order nonce, but it never reads or checks `receipt.success` before paying the reward and removing the `PendingOrder`: [3](#0-2) 

A grep across the pallet confirms `success` is decoded but never referenced anywhere in `outbound-queue-v2`'s pallet logic or its test suite, meaning the guard that would gate reward and order-removal state transitions on actual execution success does not exist. The comments in the module's doc-header describe the intended flow ("When the message has been verified and executed, the relayer will call ... to ... pay reward") — i.e., reward should only be paid when the message was executed — but the code does not enforce this; it only checks that *an event with this nonce* exists and was emitted by the correct gateway, regardless of whether the outcome recorded in that event was success or failure.

This mirrors the reported bug class exactly: a validity/condition parameter (`valid_till_block_height` there, `success` here) is present in the data model specifically to gate an action, but the pallet's dispatch logic omits the check, so state (reward payout, order removal) advances unconditionally.

### Impact Explanation
Any relayer can submit a genuine (correctly Merkle/event-proved) `InboundMessageDispatched` log where `success == false` — i.e., a message whose execution on the Gateway contract failed/reverted on Ethereum — and still collect the full relayer fee/reward that was meant to compensate for *successful* delivery, and the `PendingOrder` is permanently removed as if delivery succeeded. This breaks the "settle exactly once, to the rightful outcome" invariant for bridge payouts: rewards are minted/paid for underpriced or failed work, and the pending-order bookkeeping used to track outstanding message delivery is silently closed out even though the corresponding action never executed on Ethereum. This falls squarely into the "public underpriced work that degrades... stalls bridge processing" and duplicate/incorrect settlement categories in scope.

### Likelihood Explanation
The path is reachable by any unprivileged, signed account via the public `submit_delivery_receipt` extrinsic — no relayer allowlisting, governance, or privileged origin is required (`ensure_signed(origin)?`). The relayer only needs a legitimately provable Ethereum receipt/event proof showing `success: false` for the nonce in question (a normal, expected occurrence whenever a Gateway dispatch reverts due to gas limits or command failure), which is a realistic scenario, not a "malicious relayer/prover" assumption — the proof itself is entirely valid, only the recorded outcome is failure. This is high likelihood given failed dispatches on Ethereum are an ordinary occurrence for a cross-chain messaging system.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `true`, proceed with existing reward payment and remove the `PendingOrder` as today.
- If `false`, do not pay the reward (or pay a reduced/no fee), and decide the correct order lifecycle (e.g., still remove the order to stop retry attempts but emit a distinct `MessageDeliveryFailed`/`MessageExecutionFailed` event, or handle failed-command refund/compensation logic), rather than silently treating failed execution identically to successful delivery.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders::<T>::get(nonce)` with `fee > 0` — [4](#0-3) .
2. The message is delivered to the Ethereum Gateway but the dispatched command reverts/fails, so the Gateway contract emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer captures this real, provable event and its receipt proof and calls `submit_delivery_receipt(origin, event)` — [2](#0-1) .
4. `T::Verifier::verify` succeeds (the event is genuine), `DeliveryReceipt::try_from` decodes `success: false` correctly — [5](#0-4) .
5. `process_delivery_receipt` only checks `gateway` and looks up the `PendingOrder` by `nonce`; since `order.fee > 0`, it calls `T::RewardPayment::register_reward(...)` and removes the order — regardless of `success` being `false` — [6](#0-5) .
6. Result: the relayer is rewarded for a message whose execution on Ethereum failed, and the order bookkeeping is closed out as if delivery succeeded.

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
