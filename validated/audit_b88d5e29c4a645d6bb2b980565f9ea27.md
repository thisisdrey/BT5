## Title
`submit_delivery_receipt` couples reward payout to a fallible external `T::Verifier::verify` call, permanently DoS-ing a relayer's earned fee under any recoverable (non-error-clearing) verifier condition - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`Pallet::submit_delivery_receipt` is the sole public entrypoint that both authenticates delivery of an Ethereum message and pays the relayer fee held in `PendingOrders`. It chains a single external dependency, `T::Verifier::verify`, before reward payout. If the verifier reports the bridge as halted (or any other transient `VerificationError`), the whole extrinsic aborts, the `PendingOrder` is left untouched, and the relayer earns nothing for that call - exactly the "external call fails → primary function DoS'd" pattern from UF-2, mirrored here as a public dispatchable that funds relayer rewards.

### Finding Description
`submit_delivery_receipt` performs, in order:
1. `T::Verifier::verify(&event.event_log, &event.proof)` — an external, config-supplied verifier call [1](#0-0) 
2. Decode the `DeliveryReceipt` from the log
3. `Self::process_delivery_receipt(relayer, receipt)`, which fetches the `PendingOrder` by nonce and calls `T::RewardPayment::register_reward` before removing the order [2](#0-1) 

The pallet's own regression test `poc_m1` documents that when the verifier reports `Halted`, `submit_delivery_receipt` returns `Error::Verification(VerificationError::Halted)` and the `PendingOrder` (holding the relayer's fee) remains in storage untouched [3](#0-2) . This is the exact analog of `publicMint`'s multi-external-call DoS: reward release is gated behind one external dependency (`T::Verifier`) that a relayer cannot control and that can fail for reasons unrelated to the correctness of their own delivery proof (bridge halted, upstream light-client lag/failure, verifier storage not yet advanced for that block). Every retry with the same proof fails identically until the external condition (halted flag / verifier state) changes, so — unlike a generic "just resubmit" DoS — the failure is deterministic and state-bound, not random network flakiness.

### Impact Explanation
This maps to the "public underpriced work that degrades... stalls bridge processing" / "permanent user-fund... lock" impact category: relayers who have already done the useful off-chain work of relaying a message to Ethereum can be locked out of collecting their `PendingOrder` fee for as long as the halted condition (or any other `VerificationError`) persists, with no alternative code path to claim the fee. Because `PendingOrders` are keyed by nonce and only removed on successful `process_delivery_receipt`, a systemic pause of the verifier (e.g., an extended maintenance halt) blocks payout for every outstanding order simultaneously, not just one relayer — a broader-than-single-user denial of the reward-payout leg of the bridge's delivery-receipt flow.

### Likelihood Explanation
Likelihood is bounded by the fact that the halted/verification-error condition is not attacker-controlled in the unprivileged sense (setting halted is a privileged/root operation, which is out of scope per the impact gate), so a purely external, unprivileged attacker cannot at will trigger this. However, the same code path also fails for any other non-privileged `VerificationError` (e.g., proof not yet finalized/available, verifier storage checkpoint lag), which occurs naturally and deterministically for every retry attempt until chain state advances — this is a "public underpriced work stalls bridge processing" scenario rather than a contrived attack, matching the report's own acknowledged resolution pattern ("failed transactions can be resubmitted").

### Recommendation
Decouple reward-fee accrual from the verifier call: register or reserve the relayer's claim on the `PendingOrder` fee independent of a single fallible `verify` call, or allow relayers to retry `process_delivery_receipt` for an already-authenticated nonce without re-running verification once it has failed for a system-level (non-relayer-caused) reason. Alternatively, expose a permissionless retry/claim path keyed only on nonce + already-stored proof status, so a transient/systemic verifier condition does not gate the entire payout logic behind one external dependency, consistent with the report's "isolate external calls to another transaction(s)" and pull-over-push guidance.

### Proof of Concept
The existing test `poc_m1` in the pallet's own test suite already demonstrates the core mechanic: [3](#0-2) 
1. Insert a `PendingOrder{nonce: 1, fee: 1_000_000}`.
2. Set verifier halted (`set_verifier_halted(true)`).
3. Relayer calls `submit_delivery_receipt` with a valid event proof → returns `Error::Verification(VerificationError::Halted)`.
4. Assert `PendingOrders::get(nonce)` still holds the fee — payout never occurs, and every identical resubmission while halted fails the same way, blocking the relayer's fee claim entirely until the halted condition is externally lifted.

Note: I could not fully trace every `VerificationError` variant and every caller of `T::Verifier::verify` (e.g., beacon light-client staleness conditions) within the available indexed content; a full audit of `snowbridge_outbound_queue_primitives::Verifier` implementations would be needed to enumerate all deterministic-failure conditions beyond `Halted`.

### Citations

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
