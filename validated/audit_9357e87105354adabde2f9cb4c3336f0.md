Confirmed: no test file, benchmark, or source line in `bridges/snowbridge/pallets/outbound-queue-v2` ever inspects `receipt.success`. This is a genuine, provable analog to the report's core flaw (a check that should gate a payout/state-transition is missing or unused).

### Title
`process_delivery_receipt` pays relayer reward and settles the pending order regardless of the Ethereum-side `success` flag - (`File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` decodes an `InboundMessageDispatched` event from Ethereum into a `DeliveryReceipt` that carries a `success: bool` field indicating whether the relayed message actually executed successfully on the Gateway contract. The extrinsic verifies the merkle/header proof and the gateway address, then unconditionally pays the relayer's reward and removes the `PendingOrder` — the `success` field is decoded but never read or branched on.

### Finding Description
`DeliveryReceipt` is derived from the `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` Ethereum event and explicitly carries a `success` flag [1](#0-0) .

`submit_delivery_receipt` verifies the proof, decodes the receipt, and calls `process_delivery_receipt` [2](#0-1) .

`process_delivery_receipt` only checks the gateway address and the existence of a `PendingOrder` for the nonce; it then pays `T::RewardPayment::register_reward` with the order's fee and removes the order — `receipt.success` is never inspected: [3](#0-2) .

A `grep` over the pallet crate (source, tests, benchmarks) confirms `success` is decoded in the primitives crate but has zero references inside `outbound-queue-v2` beyond the struct definition — there is no branch, guard, or event distinguishing successful vs. failed dispatch before settlement.

This is the same broken-invariant pattern as the external report: a state-changing/value-transferring action is executed based on an incomplete/absent validation of a field that exists specifically to gate that action (there, NFT approval/ownership tracking was missing; here, delivery-success gating is missing), so the settlement path advances (`PendingOrders` removed, reward registered) without verifying the actual outcome the mechanism is supposed to confirm.

### Impact Explanation
Per the module doc, the entire purpose of `submit_delivery_receipt` is to reward relayers only "when the message has been verified and executed" [4](#0-3) . Because `success` is ignored, a message whose execution reverted/failed on the Ethereum Gateway (e.g., a command that ran out of gas, hit an invalid state, or otherwise failed) still results in the relayer being paid the full fee and the `PendingOrder` being cleared as if delivery succeeded. This is a bridge reward/settlement pallet directly handling value (`T::RewardPayment::register_reward`), so this is a real fund-accounting bug: rewards are settled to relayers independent of whether the bridged message was actually delivered, breaking the "settle exactly once to the rightful outcome" invariant for bridge reward payouts.

### Likelihood Explanation
Likelihood is Medium: any relayer that submits a message to the Ethereum Gateway can subsequently claim `submit_delivery_receipt` with the genuine, chain-verified event proof — no forgery of the proof or event is needed, since `T::Verifier::verify` still checks header/merkle validity. The relayer merely needs the underlying Ethereum transaction to have emitted `success=false` (a normal outcome of gas-limited or reverting command execution, not an attacker-controlled input), and the extrinsic remains fully payable and callable by any signed account with no additional privilege.

### Recommendation
Branch on `receipt.success` in `process_delivery_receipt`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; on `false`, remove/settle the `PendingOrder` without paying the reward (or route it to a distinct failure-handling/refund path), and emit a distinct event (e.g., `MessageDeliveryFailed`) so failed deliveries are auditable instead of being silently rewarded as if successful.

### Proof of Concept
1. A relayer relays an outbound message to the Ethereum Gateway; the Gateway attempts to execute the message's commands but execution fails/reverts on-chain, so the Gateway emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
2. The relayer obtains a valid header/merkle proof for this event (a legitimate transaction receipt — no forgery required) and calls `submit_delivery_receipt(origin, event)` on BridgeHub, mirroring the existing test flow in [5](#0-4)  but with `success: false` instead of `true` in the constructed `DeliveryReceipt`.
3. `T::Verifier::verify` succeeds (the proof is genuine), `receipt.gateway` matches `T::GatewayAddress`, and `PendingOrders::<T>::get(nonce)` returns the existing order.
4. `process_delivery_receipt` proceeds through [6](#0-5)  unconditionally, calling `register_reward` and removing the order — identical behavior to the `success: true` case, confirming the reward is paid and the order settled even though the bridged message never actually executed successfully on Ethereum.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L392-416)
```rust
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
