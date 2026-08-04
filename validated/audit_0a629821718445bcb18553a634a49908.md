### Title
Delivery receipt `success` flag is ignored, allowing reward payout and order settlement on failed Ethereum execution - (File: `bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet decodes a `DeliveryReceipt` from a verified Ethereum event log but never inspects the receipt's `success` field before crediting the relayer reward and removing the corresponding `PendingOrder`. This is a semantic analog of the reported "unnecessary low-level call" bug class: an external call/event's completion status must be validated before state is advanced, but here the decoded status is silently discarded, so a message whose Ethereum-side execution reverted is treated identically to one that succeeded.

### Finding Description
`submit_delivery_receipt` verifies the event proof, decodes it into a `DeliveryReceipt`, and calls `process_delivery_receipt`: [1](#0-0) 

`process_delivery_receipt` only checks the gateway address and the existence of a `PendingOrder` for the nonce; it never reads or validates `receipt.success`: [2](#0-1) 

Tests confirm that `DeliveryReceipt` carries a `success` field intended to represent the actual execution outcome on Ethereum (e.g. `success: true` is set explicitly in test fixtures): [3](#0-2) [4](#0-3) 

Because `success` is never consulted, `process_delivery_receipt` pays the full `order.fee` to the relayer reward account and removes the `PendingOrder` regardless of whether the receipt indicates success or failure of the corresponding command execution on Ethereum: [5](#0-4) 

The only guards present are: (a) `T::Verifier::verify` (proves the event log is genuinely from the configured gateway/chain, not that the command inside succeeded), (b) the gateway-address check, and (c) existence of a pending order for the nonce. None of these substitute for checking the execution-status flag carried by the event itself — this mirrors exactly the reported bug's core invariant break: relying on the mere occurrence of a call/event without checking its reported success outcome before advancing dependent state (reward payout, order settlement).

### Impact Explanation
This breaks the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." A relayer can obtain the full committed `fee` reward for messages whose commands reverted or failed on the Ethereum side, as long as the Gateway contract still emits a valid delivery-receipt event with `success = false`. This is an unbacked/duplicate-style payout: BridgeHub's `PendingOrders` accounting is settled and rewards distributed even though the corresponding cross-chain action did not actually complete as intended, degrading the correctness of bridge reward accounting and enabling reward extraction disconnected from genuine successful delivery.

### Likelihood Explanation
The path is reachable by any signed account (no privileged origin required) via the public `submit_delivery_receipt` extrinsic, as long as it can obtain any valid Ethereum receipt/event proof for a real gateway event — including one flagged `success = false`. No malicious peer, validator, collator, governance, or leaked key is required; only a standard relayer role interacting with the public dispatchable.

### Recommendation
Validate `receipt.success` in `process_delivery_receipt` before crediting the reward and/or before removing the `PendingOrder`, distinguishing successful vs. failed executions (e.g., emit a distinct event/path for failed deliveries, and gate `RewardPayment::register_reward` on `receipt.success == true`).

### Proof of Concept
1. A message is queued via `do_process_message`, creating a `PendingOrder { nonce, fee, .. }`.
2. The corresponding command is delivered to Ethereum and reverts/fails, but the Gateway contract still emits the delivery-receipt event with `success = false` and a valid nonce/gateway/topic.
3. A relayer collects the event log/proof and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the event genuinely occurred), `DeliveryReceipt::try_from` decodes `success = false` along with `nonce`, `reward_address`, `gateway`.
5. `process_delivery_receipt` checks only `gateway` and `PendingOrders::get(nonce)`, then unconditionally calls `T::RewardPayment::register_reward(&reward_account, .., order.fee)` and removes the order — the relayer is paid despite `success == false`. [6](#0-5)

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L104-114)
```rust
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
