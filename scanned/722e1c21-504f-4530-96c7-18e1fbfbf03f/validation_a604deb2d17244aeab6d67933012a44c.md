### Title
Relayer reward paid on outbound-queue-v2 `submit_delivery_receipt` regardless of the on-chain `success` flag in `InboundMessageDispatched` - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
The V2 outbound queue pays a relayer reward from `PendingOrders` as soon as a Merkle/receipt proof for the Ethereum `InboundMessageDispatched` event is verified, but it never inspects the `success` field of that decoded event before paying. The `DeliveryReceipt` type explicitly carries a `success: bool` field, populated straight from the Solidity event log, yet `process_delivery_receipt` ignores it entirely. This is the same class of bug as the reported oracle issue: a piece of downstream accounting state (here, relayer reward payout / order settlement) is updated using a value/condition that does not reflect the correct, narrower semantics required (payment should only occur when the message was *successfully* dispatched on Ethereum, i.e., an "admin-fee-only" analog of "delivery-confirmed-only" reward), while the code instead uses the broader/incorrect condition ("proof merely verified, and record removed") to settle state.

### Finding Description
`DeliveryReceipt` decodes the `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` event log: [1](#0-0) 

The pallet's `process_delivery_receipt` function fetches the `PendingOrder` by nonce, and if `order.fee > 0`, unconditionally calls `T::RewardPayment::register_reward` and then removes the order from `PendingOrders`, deposits `MessageDelivered`, and returns `Ok(())` — with no check on `receipt.success`: [2](#0-1) 

The only gate before this function runs is `T::Verifier::verify(&event.event_log, &event.proof)`, which validates that the Ethereum receipt/log is authentic and included in a finalized block — it says nothing about whether the dispatched command actually *succeeded* on Ethereum: [3](#0-2) 

So a genuine, unmodified relayer can submit a receipt proving that Ethereum emitted `InboundMessageDispatched(nonce, topic, success=false, reward_address)` (i.e., the destination-side command reverted/failed), and the pallet will still pay the full reward and permanently clear the pending order — exactly mirroring the reported class of bug where a state-transition value is derived without accounting for the "only when correctly qualified" condition (there: admin-fee-only D value; here: success-only settlement).

The existing tests confirm the check that *is* enforced is only proof/verifier availability (`Verification::Halted`), not delivery success — `poc_m1` and `submit_delivery_receipt_succeeds_after_unhalt` both only manipulate `set_verifier_halted`, never `success`: [4](#0-3) 

No other guard exists: a grep across the whole `outbound-queue-v2` pallet for `success`/`receipt.success` returns zero matches, confirming the field is decoded but never consulted for the payout decision.

### Impact Explanation
This directly matches the "public underpriced work / duplicate settlement / payout state advancing without a correctly-qualified success condition" impact categories. Relayers submit and pay for a genuine, valid delivery-receipt proof (the field is real and comes from an authentic Ethereum log — no forged proof, no malicious relayer/prover assumption required), yet the bridge pays out the DOT/asset reward from the pool even when the actual command execution on Ethereum reverted or failed. This is a state/fund-accounting integrity break: rewards (which come from bridge fee pots funded by users) are paid for work that did not achieve its intended outcome, degrading the token-economics of the reward scheme without needing any privileged/malicious actor — it's simply the natural outcome of any dispatched-but-failed Ethereum command, and no honest relayer needs to act maliciously to trigger it.

### Likelihood Explanation
High: any legitimately submitted `submit_delivery_receipt` extrinsic for a message whose Ethereum-side command execution reverted (a routine occurrence — e.g. destination contract call reverts, out-of-gas commands, or governance/asset commands that fail validation on the Gateway) will trigger this. No adversarial proof crafting is needed — the relayer needs only to submit the authentic receipt that Ethereum itself emitted with `success = false`. The bug is on the "happy path" for the honest-relayer, therefore likelihood of triggering is essentially guaranteed whenever any command fails downstream, and no `success`-based branch exists anywhere in the pallet to prevent it.

### Recommendation
Check `receipt.success` in `process_delivery_receipt` before calling `T::RewardPayment::register_reward`. If `success` is `false`, still remove/settle the `PendingOrder` (to avoid re-processing) but do not credit the reward (or route to a distinct "failed delivery" accounting/event path), analogous to how the audited oracle fix separated the "all fees" value from the "admin-fee-only" value before using it for a state update.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { fee, block_number }`.
2. On Ethereum, the Gateway attempts to dispatch the corresponding command but the command execution reverts (e.g., invalid Transact payload or overweight command on Ethereum side); the Gateway still emits `InboundMessageDispatched(nonce, topic, success=false, reward_address)` per its intended "always emit receipt" design.
3. A relayer (honest, no special access) crafts a standard Merkle/beacon inclusion proof for this log and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the log is real and included in a finalized block).
5. `DeliveryReceipt::try_from` decodes `success = false` correctly into the `receipt`.
6. `process_delivery_receipt` proceeds: `order.fee > 0` → `T::RewardPayment::register_reward(...)` is called and the reward is registered, `PendingOrders::remove(nonce)` executes, and `MessageDelivered` fires — all despite the failed execution on Ethereum, as shown by the absence of any `success` check in [5](#0-4) .

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L390-449)
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
}
```
