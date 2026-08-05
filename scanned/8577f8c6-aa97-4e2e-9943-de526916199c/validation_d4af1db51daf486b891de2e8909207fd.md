Confirmed: the grep shows `receipt.success` is never referenced anywhere in `outbound-queue-v2`. The `success` field decoded off the Ethereum event is dead data — `process_delivery_receipt` pays out regardless of its value.

### Title
Relayer reward is paid on Snowbridge outbound delivery receipts without checking the `success` flag of the dispatched message - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`DeliveryReceipt::try_from` decodes the Ethereum `InboundMessageDispatched(nonce, topic, success, reward_address)` event into a struct that carries an explicit `success: bool` field [1](#0-0) , but `Pallet::process_delivery_receipt` never reads that field before paying the relayer reward and clearing the pending order [2](#0-1) . This mirrors the reported bug class exactly: a "call" outcome (here, the on-chain record of message execution on Ethereum) is treated as unconditionally successful without inspecting the actual result, misleading the system into crediting a reward for work that may not have actually succeeded.

### Finding Description
The delivery flow is: BridgeHub commits an outbound message and creates a `PendingOrder{nonce, fee, ...}`; the Ethereum Gateway contract eventually emits `InboundMessageDispatched(nonce, topic, success, reward_address)` once it has attempted the message; a relayer submits this event with an execution/receipt proof via `submit_delivery_receipt` [3](#0-2) . The extrinsic:
1. Verifies the proof of the log's existence in a finalized Ethereum block (`T::Verifier::verify`).
2. Decodes the log into `DeliveryReceipt`, which includes the `success` field emitted by the Gateway.
3. Calls `process_delivery_receipt`, which checks the gateway address matches and that a `PendingOrder` exists for the nonce, then unconditionally calls `T::RewardPayment::register_reward` for `order.fee` and removes the order [4](#0-3) .

At no point is `receipt.success` inspected. The proof only establishes that the event log genuinely exists and was emitted by the correct gateway — it says nothing about whether the destination execution succeeded, because `success` is itself a payload field of that same log, and the pallet discards it. All integration tests that exercise this path construct `DeliveryReceipt { success: true, ... }` and never test the `success: false` case reaching `process_delivery_receipt` for reward suppression [5](#0-4) , confirming the field is effectively unused in this control path.

This is directly analogous to the external report: a call/interaction is deemed successful and financially settled purely because a low-level signal (the "call returned"/"the log exists") was true, without checking a more specific existence/success indicator that the protocol itself provides (contract existence vs. `receipt.success`).

### Impact Explanation
A relayer reward can be paid out for a message that the Gateway itself reported as failed. Because relayer rewards are drawn from bridge/protocol funds (`T::RewardPayment::register_reward`) and the `PendingOrder` is removed regardless, this results in payout for non-delivery/failed-execution, i.e. duplicate/incorrect settlement of value that should only be paid for successfully dispatched messages. Depending on how the Gateway can legitimately emit `success:false` (e.g., insufficient gas for command execution, command-level revert), a relayer (an ordinary unprivileged actor who need not be malicious to trigger this by merely relaying a real Ethereum event) collects a reward it should not be entitled to, draining bridge reward funds without matching value delivered.

### Likelihood Explanation
The path requires no privileged actor: any relayer who observes an `InboundMessageDispatched` event (successful or failed) with a valid execution/receipt proof can submit it via `submit_delivery_receipt`. The verification (`T::Verifier::verify`) only proves the log was really emitted by the gateway in a finalized block — it does not, and cannot, prove the semantic correctness of paying regardless of `success`. Since `success=false` is a normal, expected outcome the Gateway contract is designed to emit (not just a byzantine/adversarial condition), this is reachable in ordinary operation, not just via a compromised relayer.

### Recommendation
In `process_delivery_receipt`, gate the reward payment on `receipt.success`:
```rust
if receipt.success && order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
```
The `PendingOrder` should still be removed (the message has been finally resolved either way) and a distinct event (e.g. `MessageDeliveryFailed`) should be emitted for `success == false` so failed deliveries are observable and can be retried/handled instead of silently rewarding the relayer as if delivery succeeded.

### Proof of Concept
1. BridgeHub queues an outbound message; a `PendingOrder{nonce, fee>0}` is created via `do_process_message` [6](#0-5) .
2. The message is delivered to the Ethereum Gateway, which attempts execution and fails (e.g., an inner command reverts), emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid execution/receipt proof for this real (but `success=false`) event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (the log genuinely exists), `DeliveryReceipt::try_from` decodes `success: false` correctly.
5. `process_delivery_receipt` checks only `gateway` match and `PendingOrders::get(nonce)` — both pass — and unconditionally calls `T::RewardPayment::register_reward(...)` for `order.fee`, then removes the order and emits `MessageDelivered` [7](#0-6) .
6. Result: the relayer is rewarded and the order is closed as if delivery succeeded, even though the Gateway explicitly reported failure — reproducible by constructing a `DeliveryReceipt { success: false, .. }` and calling `process_delivery_receipt` directly, as the existing tests do for the `success: true` case [8](#0-7) .

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L426-438)
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L105-114)
```rust
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L950-968)
```rust
fn invalid_nonce_for_delivery_receipt_fails() {
	BridgeHubWestend::execute_with(|| {
		type Runtime = <BridgeHubWestend as Chain>::Runtime;

		let relayer = BridgeHubWestendSender::get();
		let reward_account = AssetHubWestendReceiver::get();
		let receipt = DeliveryReceipt {
			gateway: EthereumGatewayAddress::get(),
			nonce: 0,
			reward_address: reward_account.into(),
			topic: H256::zero(),
			success: true,
		};

		assert_err!(
			EthereumOutboundQueueV2::process_delivery_receipt(relayer, receipt),
			Error::<Runtime>::InvalidPendingNonce
		);
	});
```
