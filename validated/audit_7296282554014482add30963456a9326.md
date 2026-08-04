### Title
`process_delivery_receipt` ignores the `success` field of the delivery receipt, allowing relayer rewards for failed deliveries - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`process_delivery_receipt` in the Snowbridge V2 outbound queue pallet decodes a `DeliveryReceipt` from an Ethereum event log and pays a relayer reward and clears the pending order for a nonce, but never inspects the `success` field of that receipt [1](#0-0) . The `DeliveryReceipt` type explicitly carries a `success: bool` field decoded straight from the `InboundMessageDispatched` Ethereum event [2](#0-1) , but this user/relayer-observable value is never validated or branched on before the reward is registered and the order removed.

### Finding Description
The flow is:
1. `submit_delivery_receipt` verifies the Merkle/beacon proof for the event log and decodes it into a `DeliveryReceipt`, then calls `process_delivery_receipt` [3](#0-2) .
2. `process_delivery_receipt` checks `receipt.gateway` matches the configured gateway and resolves `reward_account` (falling back to the relayer when `reward_address` is zero) [4](#0-3) .
3. It looks up the `PendingOrders` entry by `nonce`, pays `order.fee` to `reward_account` via `T::RewardPayment::register_reward`, removes the order, and emits `MessageDelivered` [5](#0-4) .

At no point is `receipt.success` read or checked. The comment in the pallet's module docs states the receipt should be submitted "when the message has been verified **and executed**" [6](#0-5) , implying successful execution is a precondition for reward payout, yet the code pays the reward and settles/clears the order unconditionally as long as the proof and gateway/nonce checks pass — regardless of whether the Ethereum-side dispatch actually succeeded (`success == false`).

This is a direct structural analog to the external report's core defect: a decoded, attacker/relayer-influenced field (`success`, akin to the unchecked address/flag inputs in `PolicyInit`/`Policed`/`PolicedUtils`) that reaches state-mutating and value-transferring logic without validation.

### Impact Explanation
Because `success` is never checked, a relayer can submit a legitimately proven `DeliveryReceipt` for a message whose Ethereum-side dispatch failed (`success = false`) and still: (a) collect the full relayer reward for `order.fee`, and (b) cause the `PendingOrders` entry to be permanently removed, closing out settlement state for a message that was never successfully delivered/executed. This is a value-conservation and settlement-correctness violation: rewards are paid and delivery is marked complete for outcomes that did not actually complete successfully, without needing a malicious relayer/prover assumption beyond simply relaying a real (but failed) proof, which any relayer with an already-valid Merkle/beacon proof can do.

### Likelihood Explanation
The precondition is only that a real `InboundMessageDispatched` event with `success = false` exists on Ethereum (e.g., normal command execution failure at the Gateway) and that a standard relayer submits its proof — both of which are part of ordinary operation, not privileged or adversarial infrastructure control. No malicious peer/validator/governance actor is required; any signed account acting as relayer can trigger this path once a failed-dispatch event is emitted on-chain.

### Recommendation
Add an explicit check on `receipt.success` in `process_delivery_receipt` before registering the reward and/or removing the `PendingOrders` entry — e.g., only pay `order.fee` when `receipt.success == true`, and either retain/reprocess or explicitly define failure-path semantics (e.g., separate event, no reward, or reduced reward) when `success == false`, mirroring the report's recommendation to add `require`/`ensure` validation for all decoded, externally-influenced fields before they affect settlement state.

### Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrders` entry with a nonzero `fee` for `nonce = N` [7](#0-6) .
2. On Ethereum, the Gateway processes the message but the dispatched command reverts/fails, emitting `InboundMessageDispatched(nonce=N, topic, success=false, reward_address)`.
3. A relayer obtains a valid Merkle/beacon proof for this event log (this is normal relayer operation, not privileged) and calls `submit_delivery_receipt` with that proof.
4. `DeliveryReceipt::try_from` decodes `success = false` into the receipt struct [8](#0-7) , but `process_delivery_receipt` never reads this field before paying `order.fee` to `reward_account` and removing the pending order [5](#0-4) .
5. Result: the relayer is rewarded and the order is settled as if delivery succeeded, even though `success = false` was explicitly signaled in the proven event.

Note: I was unable to fully confirm from the available code/comments whether the design *intends* to reward relayers purely for delivery inclusion regardless of execution outcome (a legitimate design choice in some bridge relayer-incentive models) or whether `success` was meant to gate reward eligibility per the module doc's "verified and executed" language. This ambiguity should be verified against the Snowbridge V2 spec/audit docs before treating this as a confirmed defect rather than an intentional trade-off.

### Citations

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L44-51)
```rust
		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
	}
```
