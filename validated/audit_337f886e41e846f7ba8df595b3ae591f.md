### Title
Snowbridge outbound-queue-v2 pays relayer reward from `PendingOrders` regardless of Ethereum delivery outcome, ignoring the decoded `success` field - (File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs)

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge V2 outbound queue pallet pays out the full relayer reward attached to a `PendingOrder` as soon as a valid Ethereum receipt proof is supplied for the matching `nonce`, but it never inspects the `success` field of the decoded `DeliveryReceipt`. This mirrors the QuailFinance `claimFunds()` pattern: a claim path exists that lets a caller withdraw the full escrowed amount (`order.fee`) whenever a superficially valid proof is presented, without verifying that the underlying claimed condition (successful message execution) actually held.

### Finding Description
`submit_delivery_receipt` verifies the Merkle/receipt proof of an Ethereum log via `T::Verifier::verify`, decodes it into a `DeliveryReceipt` struct that includes a `success: bool` field [1](#0-0) , and then calls `process_delivery_receipt`. That function only checks the `gateway` address and looks up the `PendingOrder` by `nonce`; it pays the full `order.fee` to the relayer/reward account and removes the order unconditionally, with `receipt.success` and `receipt.topic` never read: [2](#0-1) 

The proof only binds `gateway` and `nonce`; it does not bind or check the `success` outcome, so any real `InboundMessageDispatched` event on Ethereum for a tracked nonce — success or failure — drains the pending reward. The doc comment for the pallet itself states the intended flow is "verify the message... fetch the pending order... pay reward with fee attached", again with no mention of gating on `success` [3](#0-2) .

Existing tests only assert that a *halted* verifier blocks payment; there is no test asserting reward is withheld when `receipt.success == false` [4](#0-3) , and the emulated integration tests only ever construct receipts with `success: true` [5](#0-4) , confirming no code path exercises the `success:false` case at all.

### Impact Explanation
This breaks the "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" invariant. A relayer can be paid the full fee for messages that failed to execute on Ethereum, causing unbacked/duplicate settlement of bridge reward funds out of the reward pot, and it removes any on-chain signal (`PendingOrders` removal / lack of retry) that the message actually needs redelivery — a bridge-state/fund-loss condition reachable by any signed account holding a legitimate (but failed-execution) Ethereum receipt, with no admin, governance, or validator collusion required.

### Likelihood Explanation
High: `submit_delivery_receipt` is a public, permissionless extrinsic (`ensure_signed` only) [6](#0-5) . An attacker/relayer merely needs to cause (or wait for) a message whose execution reverts on the Ethereum Gateway contract — a routine occurrence for underfunded/failing XCM `Transact` payloads — and then submit the resulting real (and thus provable) `InboundMessageDispatched(success=false)` event to collect the fee anyway.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only call `T::RewardPayment::register_reward` when `receipt.success == true`; on failure, still remove/settle the `PendingOrder` (or route it to a distinct failure-handling/retry path) but do not pay out the fee, and emit a distinct event (e.g. `MessageDeliveryFailed`) so downstream systems can react.

### Proof of Concept
1. A message is queued and a `PendingOrder { nonce, fee, .. }` is created via `do_process_message` [7](#0-6) .
2. The message is relayed to Ethereum but its execution reverts/fails on the Gateway, so the real `InboundMessageDispatched` event is emitted with `success = false`.
3. Any signed relayer submits `submit_delivery_receipt` with a valid Merkle/receipt proof of that real event.
4. `T::Verifier::verify` succeeds (proof is genuine), `DeliveryReceipt::try_from` decodes `success = false`, but `process_delivery_receipt` never checks it and pays `order.fee` to `reward_account` anyway [8](#0-7) .
5. Result: reward funds are paid for a failed delivery, and the `PendingOrder` is deleted, permanently losing the accounting record that the message needs redelivery/refund.

### Citations

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-419)
```rust
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

```
