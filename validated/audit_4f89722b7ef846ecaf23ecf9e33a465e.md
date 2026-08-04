Confirmed: `receipt.success` (the `success` field decoded from Ethereum's `InboundMessageDispatched` event) is only ever assigned when constructing a `DeliveryReceipt` (in `TryFrom<&Log>` for `DeliveryReceipt`) and is never read anywhere else in `outbound-queue-v2` — `process_delivery_receipt` never inspects `receipt.success`.

### Title
`process_delivery_receipt` ignores the `success` field of the delivery receipt and always pays the relayer reward - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` carries a `success: bool` field decoded from the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event, indicating whether the message actually executed successfully on the Ethereum Gateway. `Pallet::process_delivery_receipt` decodes and validates the gateway and nonce, but never checks `receipt.success` before paying the reward and clearing the pending order.

### Finding Description
`DeliveryReceipt` is decoded with an explicit `success` field: [1](#0-0) 

`process_delivery_receipt` checks the gateway address and the pending nonce, but discards `receipt.success` entirely, unconditionally paying `order.fee` to `reward_account` and removing the order: [2](#0-1) 

This is the exact analog of the reported bug class: a status/error code returned from a lower-level operation (here, the Ethereum-side dispatch outcome carried through the verified receipt) is computed and available, but the caller silently proceeds with reward settlement as if the value were unconditionally `true`. The `submit_delivery_receipt` entrypoint is a public, permissionless extrinsic reachable by any signed relayer: [3](#0-2) 

Once `T::Verifier::verify` and `DeliveryReceipt::try_from` succeed (which only prove that a genuine `InboundMessageDispatched` log for a matching nonce/gateway was included on Ethereum — not that its `success` flag was `true`), `process_delivery_receipt` pays out and settles regardless of the actual dispatch outcome recorded in that same, already-verified log.

### Impact Explanation
If message execution fails on the Ethereum Gateway (e.g., destination-side revert, out-of-gas, or invalid command) but the receipt log itself is still legitimately emitted with `success: false`, the relayer is nevertheless rewarded and the corresponding `PendingOrder` is removed from `PendingOrders`. This settles/finalizes the delivery bookkeeping and pays fees for work that did not actually complete correctly, which is the exact "duplicate/incorrect settlement despite ignored error code" class from the report — public underpriced/incorrect settlement affecting bridge processing state, since bridge reward payout state advances without confirming the payload's intended (successful) execution outcome.

### Likelihood Explanation
The path requires no privileged actor: any signed account may call `submit_delivery_receipt` with a legitimately produced Ethereum receipt for a message whose on-chain dispatch failed (this can occur naturally, not only via malicious action, whenever a real cross-chain command reverts). The verifier and decode checks that gate this function do not examine `success`, so the flawed downstream `process_delivery_receipt` logic is reached on every dispatch-failure receipt without any extra effort.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` and branch accordingly: only settle/pay full reward on `success == true`; for `success == false`, take a distinct, explicit code path (e.g., still remove/settle the order for delivery-attempt accounting but do not reward as a "MessageDelivered" success, or emit a `MessageDispatchFailed` event and apply reduced/no fee logic based on protocol intent). At minimum, ensure the `success` value is consciously handled rather than silently discarded, consistent with the guidance to "always check the return/status code" from the source report.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce]` with a non-zero `fee`. [4](#0-3) 
2. On Ethereum, the Gateway attempts dispatch and it reverts/fails, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid receipt/execution proof for that (legitimately emitted, failed) log and calls `submit_delivery_receipt`. Verification succeeds because it only checks proof validity, not the `success` flag. [3](#0-2) 
4. `DeliveryReceipt::try_from` decodes `success: false` correctly, but `process_delivery_receipt` never reads it. [5](#0-4) 
5. The relayer is paid `order.fee` and `PendingOrders[nonce]` is removed as if the message were successfully delivered. [6](#0-5) 

Existing tests only cover the `Halted`/gateway/nonce checks and do not assert any different behavior for `success: false` receipts (all crafted test receipts set `success: true`), confirming this path is unguarded. [7](#0-6)

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-416)
```rust
// Reward processing must be blocked while the bridge is halted: `submit_delivery_receipt`
// should not pay out `PendingOrder` fees if the verifier reports the bridge as halted.
#[test]
fn poc_m1() {
	new_tester().execute_with(|| {
		let nonce = 1;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_event_proof());

		set_verifier_halted(true);

		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);

		let order_after = PendingOrders::<Test>::get(nonce).expect("order still present");
		assert_eq!(order_after.fee, fee);

		set_verifier_halted(false);
	});
}
```
