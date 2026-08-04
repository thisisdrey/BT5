## Finding

### Title
`OutboundQueueV2::process_delivery_receipt` pays relayer reward and settles the pending order without checking `DeliveryReceipt.success` - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet reads the `nonce`, `gateway`, and `reward_address` fields off an incoming `DeliveryReceipt`, but never inspects the `success` field before paying the relayer reward and permanently deleting the `PendingOrder`. This mirrors the `ControllerPeggedAssetV2::triggerEndEpoch` bug class: a settlement function assumes the "happy path" condition (successful delivery) always holds and finalizes payout/state without gating on the condition that should route to a different (non-payout) outcome.

### Finding Description
`process_delivery_receipt` is the function that turns a verified Ethereum delivery receipt into an on-chain relayer reward and removes the corresponding `PendingOrder`: [1](#0-0) 

Concretely:
```rust
pub fn process_delivery_receipt(...) -> DispatchResult {
    ensure!(T::GatewayAddress::get() == receipt.gateway, Error::<T>::InvalidGateway);
    let reward_account = if receipt.reward_address == [0u8; 32] { relayer } else { receipt.reward_address.into() };
    let nonce = receipt.nonce;
    let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
    if order.fee > 0 {
        T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
    }
    <PendingOrders<T>>::remove(nonce);
    Self::deposit_event(Event::MessageDelivered { nonce });
    Ok(())
}
```
The `DeliveryReceipt` type carries an explicit `success: bool` field — this is visible from how test code and emulated integration tests construct it (`success: true`) and from the struct definition location: [2](#0-1) [3](#0-2) 

The pallet's own module docs describe the intended flow as "Fetch the pending order by nonce of the message, pay reward with fee attached in the order" once the message "has been verified and executed" on Ethereum: [4](#0-3) 

However nothing in `process_delivery_receipt` conditions the `register_reward` call or the `PendingOrders::remove` on `receipt.success`. A receipt whose Merkle/event-log proof verifies correctly (i.e. it genuinely corresponds to a real Ethereum transaction receipt for that nonce) but which encodes `success: false` — meaning the Gateway's dispatch of the command reverted on the Ethereum side — is treated identically to a successful delivery: the relayer is still rewarded and the order is irrevocably removed from `PendingOrders`.

This is the direct structural analog of the reported bug: just as `triggerEndEpoch` resolved an epoch via the "funds transferred" path even when the null-epoch condition should have routed to `triggerNullEpoch` (no transfer), `process_delivery_receipt` resolves a delivery receipt via the "reward paid + order settled" path even when the failure condition encoded in the receipt itself (`success == false`) should route to a different outcome (no reward, and/or a path allowing the order/message to be reprocessed or refunded).

### Impact Explanation
- Relayer rewards are paid for deliveries that did not actually succeed on Ethereum, directly draining the reward budget/fee pot for work that was not completed as promised.
- Because `PendingOrders::remove(nonce)` runs unconditionally, the order state is permanently destroyed on a failed delivery, so there is no path left to retry, refund the fee to the sender, or otherwise reconcile the outbound message's real-world outcome — a form of the "duplicate/incorrect settlement" and "underpriced/mis-priced work" impact classes called out in the assessment scope (bridge reward payout settling to the wrong condition, permanent loss of recoverable state).
- Repeated exploitation lets any relayer who can produce a technically-valid-but-`success:false` receipt (or who colludes with/observes an Ethereum-side revert) collect fees for undelivered messages, degrading the Snowbridge outbound processing/reward economics without needing a malicious validator, governance actor, or leaked key — an ordinary relayer submitting a real, unmodified but failed receipt is sufficient.

### Likelihood Explanation
Any relayer that observes an Ethereum transaction where the Gateway's execution of a queued command reverted (a normal, permissionless, expected occurrence — Gateway-side execution can fail for many reasons unrelated to the relayer) can submit that receipt via the (external, presumably `submit_delivery_receipt`) extrinsic. Since the receipt's proof legitimately corresponds to a real event log, proof verification (`Error::Verification`, gateway-address check) passes; the only field distinguishing "delivery worked" from "delivery failed" — `success` — is simply never read by the settlement logic. No special privileges, timing races, or malicious infrastructure are required, making this readily and repeatably triggerable by a normal relayer.

### Recommendation
Gate the reward payment and terminal removal of the pending order on `receipt.success`:
- If `receipt.success == false`, do not call `T::RewardPayment::register_reward`.
- Decide and implement the correct non-happy-path settlement (e.g., emit a distinct `MessageDeliveryFailed` event, and/or retain/refund the order so the fee is not simply forfeited or awarded incorrectly), analogous to how the audited fix required `triggerEndEpoch` to revert (route to a distinct code path) whenever the null-epoch condition holds instead of always executing the transfer path.

### Proof of Concept
1. A message is queued via `process_message_impl`, creating `PendingOrders[nonce]` with a non-zero `fee`. [5](#0-4) 
2. On Ethereum, the Gateway attempts to execute the corresponding command and it reverts (e.g. due to an out-of-gas or business-logic revert unrelated to the relayer's honesty), producing a real transaction receipt/event log with a "failure" outcome.
3. The relayer builds a `DeliveryReceipt { gateway, nonce, reward_address, topic, success: false }` from this genuine event log and submits it through the verification path.
4. `process_delivery_receipt` is invoked; it does not read `success`, so:
   - `T::RewardPayment::register_reward(&reward_account, DefaultRewardKind, order.fee)` still executes, paying the relayer as if delivery succeeded.
   - `<PendingOrders<T>>::remove(nonce)` still executes, permanently discarding any chance to retry, refund, or otherwise handle the failed delivery.
5. Result: relayer reward is paid, and the associated fee/state is settled for a delivery that never actually completed on Ethereum — an unconditional-success settlement bug identical in shape to the `triggerEndEpoch`/null-epoch report.

Note: I was unable to view the exact body of the `submit_delivery_receipt` extrinsic or the `DeliveryReceipt`/verification struct definitions (`bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs`) in full due to index truncation, so I cannot 100% confirm whether upstream verification independently rejects `success: false` receipts before reaching `process_delivery_receipt`. Based on all directly observed code — the `process_delivery_receipt` function body itself, its module-level design docs, and every test/integration call site — `success` is never referenced in the settlement logic, which is the core evidence for this finding. If a Devin session is opened, confirming the full verification path in `submit_delivery_receipt` and the `delivery_receipt.rs` primitive would fully close out any remaining uncertainty.

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L418-443)
```rust
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
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L398-426)
```rust
	BridgeHubWestend::execute_with(|| {
		type RuntimeEvent = <BridgeHubWestend as Chain>::RuntimeEvent;

		// Check that the Ethereum message was queue in the Outbound Queue
		assert_expected_events!(
			BridgeHubWestend,
			vec![RuntimeEvent::EthereumOutboundQueueV2(snowbridge_pallet_outbound_queue_v2::Event::MessageQueued{ .. }) => {},]
		);

		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 1,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		// Submit a delivery receipt
		assert_ok!(EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt));

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { .. }) => {},
			]
		);
	});
```
