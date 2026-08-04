Confirmed: no test in the outbound-queue-v2 suite references `success` at all, and `process_delivery_receipt` never reads `receipt.success` before paying the reward. This is the concrete analog to the report's "event-based vs. result-based" bypass — the code trusts the mere occurrence of a cryptographically-proven event (an `if`-style existence check on a verified proof) rather than binding the payout decision to the actual outcome field the event carries.

### Title
Relayer reward is paid out on `submit_delivery_receipt` regardless of the Ethereum-side delivery `success` flag - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
The Snowbridge outbound queue v2 pallet's `process_delivery_receipt` pays the relayer reward and closes the `PendingOrder` for a nonce purely based on the existence of a verified `InboundMessageDispatched` event log, without checking the `success` boolean that the very same event carries. This mirrors the report's core flaw: an "event-based" check (did a proof verify / did an event occur) is used to authorize a sensitive action, instead of a "result-based" check that binds the action to the actual outcome (`success == true`) the proof attests to.

### Finding Description
The Ethereum `Gateway` contract emits `InboundMessageDispatched(uint64 indexed nonce, bytes32 topic, bool success, bytes32 reward_address)` [1](#0-0) , and this is decoded into a `DeliveryReceipt` struct that explicitly carries `pub success: bool` [2](#0-1) .

The public extrinsic `submit_delivery_receipt` verifies the Merkle/receipt proof via `T::Verifier::verify`, decodes the event into a `DeliveryReceipt`, and calls `process_delivery_receipt` [3](#0-2) . Inside `process_delivery_receipt`, the code checks the gateway address, resolves the reward account, fetches the `PendingOrder` by nonce, and unconditionally pays the fee whenever `order.fee > 0`, then removes the order and emits `MessageDelivered` [4](#0-3) . At no point is `receipt.success` read or checked.

Cryptographic proof verification (the "result-based" primitive here) only proves that the event log genuinely occurred in a finalized Ethereum block — it says nothing about whether the destination-side dispatch actually succeeded. The `success` field is exactly the field designed to convey that outcome, analogous to the biometric authentication *result* that should gate a sensitive action. By ignoring it, the pallet behaves like the report's "event-based" authentication: it authorizes payout on the mere occurrence of an (attacker-uncontrollable but outcome-independent) event, not on the semantic result the event is meant to certify.

### Impact Explanation
Any account can call the permissionless `submit_delivery_receipt` extrinsic with a valid proof for a genuine `InboundMessageDispatched` log whose `success` is `false` (i.e., the message dispatch reverted or failed on the Ethereum side for any reason — insufficient gas at the destination, a reentrant/failing command, etc.) and still have the pallet: (1) pay out the relayer reward from `T::RewardPayment`, and (2) permanently remove the `PendingOrder`, emitting `MessageDelivered`. This causes bridge funds (relayer reward pool) to be paid for work that did not actually complete, and it forecloses any future accounting recovery/reprocessing for that nonce since the order is deleted regardless of outcome. This is a direct payout/settlement-correctness violation — value is not "settled exactly once to the rightful beneficiary and amount" as required, since a failed delivery is treated identically to a successful one.

### Likelihood Explanation
No malicious relayer, validator, or governance action is required — this is triggered by the pallet's own logic on any legitimately proven event, including entirely organic Ethereum-side failures (out-of-gas commands, reverting destination logic) which are expected to occur in normal bridge operation. Any unprivileged account holding the proof (which is public, since it's just a normal Ethereum receipt/event) can submit it and receive the payout, or an honest relayer submitting a real failed-delivery receipt will simply get paid anyway — either way the invariant is broken with default, non-privileged capabilities.

### Recommendation
Add `ensure!(receipt.success, Error::<T>::DeliveryFailed)` (or an equivalent conditional payout path) in `process_delivery_receipt` before crediting `T::RewardPayment::register_reward`. If failed deliveries should still allow relayers a smaller "attempt" reward or a retry mechanism, that policy must be explicit and bounded, not simply payment identical to success. At minimum, do not silently drop the `PendingOrder` and pay full reward when `success == false`.

### Proof of Concept
1. On Ethereum, a `v2_sendMessage`-originated command is dispatched by the Gateway, but the destination execution reverts (e.g., insufficient `execution_fee`/gas for the command), causing the Gateway to emit `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
2. Any account (not necessarily the relayer who submitted the original message) constructs an `EventProof` from this genuine, finalized event and its valid receipt proof.
3. It calls `EthereumOutboundQueueV2::submit_delivery_receipt(origin, Box::new(event))`.
4. `T::Verifier::verify` succeeds (the event/proof is real) [5](#0-4) .
5. `process_delivery_receipt` finds the `PendingOrder`, sees `order.fee > 0`, and calls `T::RewardPayment::register_reward` unconditionally, then removes the order and emits `MessageDelivered` [6](#0-5)  — despite `receipt.success == false`. The existing test suite confirms `success` is never asserted against in this flow, and the mock event log in tests hardcodes payloads without ever varying/checking the `success` bit against pallet behavior [7](#0-6) .

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L374-416)
```rust
// A valid, decodable `InboundMessageDispatched` event log emitted by the mock Gateway.
// Nonce (indexed topic) is 0, matching a `PendingOrder` inserted with nonce=0 in tests.
fn mock_valid_event_proof() -> EventProof {
	let mut event = mock_event_proof();
	event.event_log = snowbridge_outbound_queue_primitives::Log {
		address: hex!("b1185ede04202fe62d38f5db72f71e38ff3e8305").into(),
		topics: vec![
			hex!("8856ab63954e6c2938803a4654fb704c8779757e7bfdbe94a578e341ec637a95").into(),
			hex!("0000000000000000000000000000000000000000000000000000000000000000").into(),
		],
		data: hex!("907b6ec7bf3f2496ef79238e0fb19e032bfe444c7ffe906bd340c6c4ffe8511f0000000000000000000000000000000000000000000000000000000000000001d43593c715fdd31c61141abd04a99fd6822c8558854ccde39a5684e7a56da27d").into(),
		tx_index: 0,
	};
	event
}

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
