## Analysis

The external report's core broken invariant: **a state-mutating fund-payout path fails to check a status flag that determines whether the payout is actually warranted**, allowing a payout/state-advance to proceed as if the precondition were satisfied when it was not.

I found a direct structural analog in the Snowbridge outbound queue v2 pallet: `process_delivery_receipt` decodes a `DeliveryReceipt` that carries an explicit `success: bool` field (indicating whether the message was actually executed successfully on Ethereum), but never inspects that field before paying out the relayer reward and permanently deleting the pending order.

### Title
Snowbridge `submit_delivery_receipt` pays relayer rewards and settles the order regardless of on-chain delivery `success` status - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge outbound-queue-v2 pallet fetches the `PendingOrder` for a nonce, unconditionally pays out `order.fee` as a relayer reward, and removes the order from storage — without ever checking `receipt.success`, even though the `DeliveryReceipt` type explicitly carries this field decoded from the Ethereum `InboundMessageDispatched` event log.

### Finding Description
The `DeliveryReceipt` struct decoded from the Ethereum gateway's `InboundMessageDispatched` event contains a `success` field [1](#0-0) , populated directly from the emitted event's `success` value [2](#0-1) .

`submit_delivery_receipt` verifies the cryptographic proof of the event log and decodes it into a `DeliveryReceipt`, then calls `process_delivery_receipt` [3](#0-2) . Inside `process_delivery_receipt`, the gateway address is checked, the pending order is looked up by nonce, and if `order.fee > 0` the reward is unconditionally registered via `T::RewardPayment::register_reward`, after which the order is removed from `PendingOrders` and a `MessageDelivered` event is emitted [4](#0-3) . At no point is `receipt.success` read or branched on.

This exactly parallels the reported bug class: a payout/settlement function (`withdrawFunds` / `process_delivery_receipt`) advances irreversible state (fund transfer / order removal) based only on the *existence* of a record (the loan / the pending order), not on the *status flag* that should gate the payout (`startDate` funded flag / `success` delivery flag). Existing guards — the gateway check and the `PendingOrders::get(nonce).ok_or(...)` existence check — verify authenticity and that an order is still outstanding, but neither guard inspects whether the message was actually executed successfully on Ethereum.

### Impact Explanation
A relayer can submit a genuine, correctly-signed proof for a `InboundMessageDispatched` event where `success == false` (i.e., the command execution failed/reverted on the Ethereum Gateway contract) and still receive the full relayer fee, and the `PendingOrder` is deleted as if delivery had fully succeeded. This breaks the intended incentive/settlement model of the bridge reward system: rewards are meant to compensate for *successful* delivery and dispatch, not merely for a relayer submitting a receipt. Once the order is removed, there is no other path to retry, re-queue, or re-reward correctly, so the discrepancy is permanent and unbacked reward mint/payout occurs for failed work — a duplicate-settlement/underpriced-work condition affecting Snowbridge BridgeHub reward accounting.

### Likelihood Explanation
This is reachable by any unprivileged relayer through the public `submit_delivery_receipt` extrinsic once a legitimate (verifier-passing) event proof exists showing `success: false` — no malicious peer, validator, or governance action is required; a normal failed dispatch on the Ethereum side (e.g., an underfunded/failing command) naturally produces such an event, and the relayer merely submits it as-is.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success` before registering the reward: only call `T::RewardPayment::register_reward` when `receipt.success` is `true`; if `false`, still remove/settle the `PendingOrder` (to avoid unbounded growth) but emit a distinct event (e.g., `MessageDeliveryFailed`) and skip the reward payment, or apply a reduced/alternate compensation policy consistent with bridge design intent.

### Proof of Concept
1. A message is queued via `do_process_message`, creating `PendingOrders[nonce] = { nonce, fee: F, block_number }` [5](#0-4) .
2. On Ethereum, the Gateway contract attempts dispatch but the command fails/reverts, emitting `InboundMessageDispatched(nonce, topic, success=false, reward_address)`.
3. A relayer obtains a valid Merkle/receipt proof for this event and calls `submit_delivery_receipt(event)`.
4. `T::Verifier::verify` succeeds (proof is legitimate), `DeliveryReceipt::try_from` decodes `success: false` correctly [6](#0-5) .
5. `process_delivery_receipt` never reads `receipt.success`; because `order.fee > 0`, it calls `T::RewardPayment::register_reward(&reward_account, ..., order.fee)`, removes `PendingOrders[nonce]`, and emits `MessageDelivered { nonce }` exactly as it would for a genuinely successful delivery [7](#0-6) .
6. The relayer is paid for a message that failed to execute on Ethereum, and the associated test suite (`submit_delivery_receipt_succeeds_after_unhalt`, `snowbridge_v2_outbound.rs` reward tests) only exercises the `success: true` case, confirming the `false` branch is untested and unguarded [8](#0-7) [9](#0-8) .

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

**File:** bridges/snowbridge/primitives/outbound-queue/src/v2/delivery_receipt.rs (L35-51)
```rust
impl TryFrom<&Log> for DeliveryReceipt {
	type Error = DeliveryReceiptDecodeError;

	fn try_from(log: &Log) -> Result<Self, Self::Error> {
		let topics: Vec<B256> = log.topics.iter().map(|x| B256::from_slice(x.as_ref())).collect();

		let event = InboundMessageDispatched::decode_raw_log_validate(topics, &log.data)
			.map_err(|_| DeliveryReceiptDecodeError::DecodeLogFailed)?;

		Ok(Self {
			gateway: log.address,
			nonce: event.nonce,
			topic: H256::from_slice(event.topic.as_ref()),
			success: event.success,
			reward_address: event.reward_address.0,
		})
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs (L446-480)
```rust
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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/test.rs (L420-443)
```rust
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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L407-418)
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
