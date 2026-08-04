## Analysis

**Bug-class reduction:** The Sherlock report's core broken invariant is *"the code assumes a companion interface signature/semantics that the actual counterpart does not honor, so completion state is accepted/paid out without matching real completion."* The strongest local analog in this repository is in the Snowbridge V2 outbound queue reward-settlement flow, which advances payout state without checking the actual execution-outcome flag carried in the verified proof — a direct violation of the required pivot: *"payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."*

### Title
Snowbridge V2 `process_delivery_receipt` pays and settles relayer reward without checking Ethereum execution `success` flag - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The `DeliveryReceipt` decoded from the Ethereum `InboundMessageDispatched` event carries an explicit `success: bool` field indicating whether the dispatched command actually executed successfully on Ethereum. `Pallet::process_delivery_receipt` verifies the event proof, decodes the receipt, and then unconditionally pays out the full `PendingOrder.fee` reward and removes the order — it never inspects `receipt.success`.

### Finding Description
The event definition explicitly encodes execution outcome: [1](#0-0) 

`process_delivery_receipt` uses `receipt.gateway`, `receipt.reward_address`, and `receipt.nonce`, but `receipt.success` (and `receipt.topic`) is decoded and then simply discarded — no branch in the function reads it: [2](#0-1) 

The pallet's own module documentation describes the intended flow as: verify proof, "fetch the pending order by nonce ... pay reward with fee attached in the order," then remove the order — with no mention of gating on dispatch success: [3](#0-2) 

Existing tests only cover the "bridge halted" guard and the happy path; none exercise `receipt.success == false`, confirming the success flag is untested and unused: [4](#0-3) 

The extrinsic entry point that reaches this code only requires a signed origin (any relayer) plus a valid proof — no privileged actor is involved: [5](#0-4) 

Because the light-client/Beefy proof only attests that the *log was emitted*, not that the command *succeeded*, any relayer can submit a valid receipt for a message whose remote execution reverted on Ethereum (e.g., insufficient gas supplied by the relayer at execution time, or the command itself reverting) and still collect the full fee and permanently clear the `PendingOrder`, exactly matching the required pivot that "payout state must only advance after ... execution ... succeed[s] atomically."

### Impact Explanation
This lets an unprivileged relayer be rewarded for message deliveries that did not actually execute successfully on the Ethereum side, and the `PendingOrder` is removed regardless — meaning there is no remaining mechanism to retry or re-attribute the reward once the (failed) receipt is processed. This is a fund-payout correctness bug: the wrong condition (proof-of-log-emission) is treated as the settlement condition (proof-of-successful-execution), causing reward payout to a party for outcomes that were not actually delivered as promised, i.e., degraded/underpriced-work-like impact on the bridge's economic guarantees and treasury‑style payout logic.

### Likelihood Explanation
Any relayer that already runs the required infrastructure to submit `submit_delivery_receipt` (which is the standard permissionless flow) can trigger this any time a dispatched command fails on Ethereum (a common enough occurrence for underpriced/complex commands) — no governance, validator, or malicious-peer assumption is needed, and the call path is a normal public extrinsic reachable with a signed origin.

### Recommendation
In `Pallet::process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; for `success == false`, either withhold/slash the fee, refund it to the original sender, or route to a distinct failure-settlement path, while still removing/marking the `PendingOrder` to avoid replay — ensuring payout state advances only after confirmed successful execution as required.

### Proof of Concept
1. A message is queued via `SendMessage::deliver`, creating `PendingOrders[nonce] = { fee, ... }` (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:426-436`).
2. On Ethereum, the dispatched command reverts (e.g., out-of-gas on a heavy XCM `Transact`), but the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. Any relayer builds a valid proof for this log and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the log genuinely exists), `DeliveryReceipt::try_from` decodes `success: false` successfully, and `process_delivery_receipt` proceeds to `T::RewardPayment::register_reward(&reward_account, ..., order.fee)` and removes the `PendingOrder` — identical to the `success: true` path, because `receipt.success` is never read (`bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs:445-480`).

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-448)
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

// After governance resumes the bridge, legitimate delivery receipts flow through again:
// the order is paid out and removed from storage.
#[test]
fn submit_delivery_receipt_succeeds_after_unhalt() {
	new_tester().execute_with(|| {
		let nonce = 0;
		let fee: u128 = 1_000_000;
		let order = PendingOrder { nonce, fee, block_number: System::block_number() };
		PendingOrders::<Test>::insert(nonce, order);

		let relayer: AccountId32 = [7u8; 32].into();
		let origin = RuntimeOrigin::signed(relayer);
		let event = Box::new(mock_valid_event_proof());

		// Bridge halted — receipt rejected, order untouched.
		set_verifier_halted(true);
		assert_noop!(
			OutboundQueue::submit_delivery_receipt(origin.clone(), event.clone()),
			Error::<Test>::Verification(VerificationError::Halted)
		);
		assert!(PendingOrders::<Test>::get(nonce).is_some());

		// Bridge resumed — same receipt succeeds and the order is settled.
		set_verifier_halted(false);
		assert_ok!(OutboundQueue::submit_delivery_receipt(origin, event));
		assert!(PendingOrders::<Test>::get(nonce).is_none());

		System::assert_has_event(mock::RuntimeEvent::OutboundQueue(Event::MessageDelivered {
			nonce,
		}));
	});
```
