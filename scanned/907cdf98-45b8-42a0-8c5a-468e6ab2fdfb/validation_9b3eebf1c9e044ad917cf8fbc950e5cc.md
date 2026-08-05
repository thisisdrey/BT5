## Finding

The Snowbridge `outbound-queue-v2` pallet decodes a `success` field from the Ethereum `InboundMessageDispatched` event but never checks it before releasing the relayer reward, which is a real, provable local analog to the report's core pattern: **a piece of state that should gate an epoch/settlement-critical action is decoded/tracked but never actually wired into the guard that controls it.**

### Title
Relayer reward paid on `submit_delivery_receipt` regardless of Ethereum dispatch outcome (`success` flag ignored) - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`DeliveryReceipt` decoded from the `InboundMessageDispatched` Ethereum event carries a `success: bool` field indicating whether the message dispatch on Ethereum succeeded [1](#0-0) . `Pallet::process_delivery_receipt`, which is reached from the public extrinsic `submit_delivery_receipt`, verifies the gateway address and looks up the `PendingOrder` by nonce, then unconditionally pays the relayer's fee and removes the order — it never inspects `receipt.success` [2](#0-1) . A `grep` across the pallet confirms `success` is never referenced anywhere in `bridges/snowbridge/pallets/outbound-queue-v2/` outside of the struct decode.

### Finding Description
The pallet doc-comment itself states the intended flow: "When the message has been verified and executed, the relayer will call... `submit_delivery_receipt` to... Fetch the pending order by nonce... pay reward with fee attached" [3](#0-2) . This implies reward is meant to be conditioned on successful execution on Ethereum. The `success` boolean exists precisely to communicate this outcome from the on-chain Ethereum event log. However `process_delivery_receipt` only validates:
- `receipt.gateway == T::GatewayAddress::get()`
- `PendingOrders::<T>::get(nonce)` exists

and then pays `order.fee` to the relayer unconditionally [4](#0-3) . There is no branch on `receipt.success`, so a `DeliveryReceipt` reporting `success: false` (the Ethereum-side dispatch failed) is treated identically to `success: true` for payout purposes — the pallet advances the payout state (removes `PendingOrders` entry, registers the reward) purely on proof validity + nonce match, not on the semantic outcome of dispatch.

This directly violates the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" — here the settlement (reward registration) advances even when execution explicitly failed, because the only guard used is `Verifier::verify` (a proof-validity check) plus nonce presence, not the decoded outcome field.

### Impact Explanation
Any account holding a valid Ethereum receipt proof for a failed `InboundMessageDispatched` event (success=false) can submit it via the permissionless, signed `submit_delivery_receipt` extrinsic and still collect the full relayer fee that was reserved when the message was queued [5](#0-4) . This breaks the fee-for-successful-delivery incentive model: relayers are paid the same whether or not the cross-chain command actually executed on Ethereum, undermining the bridge's delivery guarantees and allowing systematic over-payment of `RewardPayment::register_reward` for work that did not achieve its stated purpose, i.e., a wrong/underserved payout condition on a public entrypoint with no privileged actor required.

### Likelihood Explanation
High likelihood: `submit_delivery_receipt` is a plain signed extrinsic [5](#0-4) , callable by any account, with no additional check tying reward eligibility to `receipt.success`. Any relayer whose message dispatch fails on Ethereum for any reason (e.g. reentrancy guard, insufficient gas provided by attacker-controlled commands, contract-side revert) can still submit the resulting failure receipt and be paid in full, requiring no admin, governance, or validator collusion.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before calling `T::RewardPayment::register_reward`: only pay the fee when `success == true`; on `false`, remove/settle the `PendingOrder` without paying the reward (or handle it via a distinct failure-accounting path), so payout state advances only after execution is confirmed successful, consistent with the pallet's own documented flow [6](#0-5) .

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders::<T>::insert(nonce, PendingOrder { nonce, fee, block_number })` with `fee > 0` [7](#0-6) .
2. On Ethereum, the message dispatch fails; the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer builds a valid receipt proof for this event and calls `submit_delivery_receipt` [5](#0-4) .
4. `T::Verifier::verify` succeeds (the event genuinely occurred), `DeliveryReceipt::try_from` decodes `success: false` successfully [8](#0-7) .
5. `process_delivery_receipt` finds the `PendingOrder`, sees `order.fee > 0`, and calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` — paying the relayer despite `success == false` — then removes the order [9](#0-8) .

The existing test suite exercises the halted/unhalted paths and nonce/order presence [10](#0-9)  but no test asserts reward is withheld when `success: false`, confirming the guard is absent by design/oversight.

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-52)
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
