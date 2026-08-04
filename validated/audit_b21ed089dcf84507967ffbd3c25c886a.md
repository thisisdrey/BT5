## Title
`submit_delivery_receipt` pays relayer reward and clears the pending order regardless of the `DeliveryReceipt.success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`submit_delivery_receipt` is a public, unprivileged (`ensure_signed`) extrinsic that verifies an Ethereum execution proof and then calls `process_delivery_receipt`. The decoded `DeliveryReceipt` carries a `success: bool` field taken directly from the `InboundMessageDispatched` event on Ethereum, but `process_delivery_receipt` never inspects it before paying the reward and deleting the `PendingOrder`.

### Finding Description
The Ethereum-side event `InboundMessageDispatched(nonce, topic, success, reward_address)` explicitly signals whether the message's commands executed successfully on the Gateway contract [1](#0-0) . This field is faithfully decoded into `DeliveryReceipt::success` [2](#0-1) .

However, `process_delivery_receipt` — invoked from the public `submit_delivery_receipt` extrinsic after proof verification — only checks the gateway address and the existence of the `PendingOrders` entry; it never reads `receipt.success`. It unconditionally pays the fee via `T::RewardPayment::register_reward` and removes the pending order: [3](#0-2) 

The call path is: `submit_delivery_receipt` (signed, unprivileged) → `Verifier::verify` (checks the receipt log/proof is authentic and belongs to a real Ethereum transaction) → `DeliveryReceipt::try_from` (decodes `success`) → `process_delivery_receipt` (ignores `success`) [4](#0-3) .

This mirrors the report's core broken invariant: a state-mutating handler that swaps/settles a critical piece of protocol state (here, the reward payout + pending-order lifecycle) without validating a value that is supposed to gate that mutation (there, `quantammAdmin`/rule; here, `success`). No admin action or malicious peer/relayer is needed — any relayer holding a genuine execution proof of a message whose Ethereum-side commands reverted (e.g., `CallContract` target reverts, insufficient forwarded gas within the gas meter's bound, or any other on-chain failure that still emits `InboundMessageDispatched` with `success = false`) can submit that receipt and still be rewarded in full, exactly as if the message had succeeded.

### Impact Explanation
This breaks the intended settlement invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Here the pending-order is finalized and the fee is paid even when execution demonstrably failed:
- **Duplicate/incorrect settlement**: the relayer receives the fee for work whose outcome doesn't match what governance/users paid for (the fee is meant to compensate for successful relaying).
- **No retry path is enforced**: since `PendingOrders` is removed on any submitted receipt regardless of `success`, a failed message cannot be re-attempted/re-rewarded through the normal pending-order accounting, and the sender/user whose message failed has no recourse — this can silently misallocate protocol-controlled reward funds over time.
- This is a public, unprivileged entrypoint (`ensure_signed`), not an admin/governance function, matching the requirement that only unprivileged-triggerable analogs be reported.

### Likelihood Explanation
High feasibility: any relayer that already participates in the normal `submit_delivery_receipt` flow can trigger this by relaying a message whose Ethereum execution fails (which is a plausible/likely occurrence for `CallContract`, `Upgrade`, or asset-mint commands with insufficient gas or third-party revert conditions) and receive a payout identical to a success case. No forged proof, no colluding validator, and no admin action are required — only a legitimately obtained proof for a failed dispatch.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`:
- If `success == true`, keep current behavior (pay reward, remove `PendingOrders` entry, emit `MessageDelivered`).
- If `success == false`, do not pay the reward (or pay a reduced/no fee per protocol design), and either keep the order for potential retry/refund handling or transition it into a distinct "failed" state/event (e.g., emit `MessageDispatchFailed { nonce }`) rather than silently treating it identically to a successful delivery.

### Proof of Concept
1. A message is enqueued via `do_process_message`, creating `PendingOrders[nonce]` with a non-zero `fee` [5](#0-4) .
2. On Ethereum, the Gateway executes the message's commands but they revert/fail (e.g., a `CallContract` target reverts), and the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a genuine receipt/execution proof for this event and calls `submit_delivery_receipt(origin, event)` as a normal signed account.
4. `Verifier::verify` succeeds (the proof is authentic), `DeliveryReceipt::try_from` decodes `success = false` correctly, but `process_delivery_receipt` never checks it: `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` is called and `PendingOrders::remove(nonce)` executes exactly as it would for a successful delivery [6](#0-5) .
5. Result: the relayer is fully rewarded for a message that failed on Ethereum, and the pending order is deleted, closing off any future accounting for the failure.

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
