### Title
`process_delivery_receipt` pays relayer reward and closes the `PendingOrder` even when the Ethereum delivery failed - ([File: bridges/snowbridge/pallets/outbound-queue-v2/src/lib.rs])

### Summary
`Pallet::process_delivery_receipt` in the Snowbridge v2 outbound queue decodes a `DeliveryReceipt` (which explicitly carries a `success: bool` field taken from the `InboundMessageDispatched(uint64 nonce, bytes32 topic, bool success, bytes32 reward_address)` Ethereum event) but never inspects `receipt.success` before paying the relayer reward and permanently removing the `PendingOrder`.

### Finding Description
The delivery-receipt event is decoded with a `success` flag by design: [1](#0-0) 

`submit_delivery_receipt` verifies the receipt's Merkle/receipt proof (so the event content itself cannot be forged) and forwards the decoded `DeliveryReceipt` to `process_delivery_receipt`: [2](#0-1) 

`process_delivery_receipt` only checks the gateway address and looks up the `PendingOrder` by `nonce`. It then unconditionally pays `order.fee` to the reward account and removes the order — `receipt.success` is read into the struct but is never matched against `true`/`false` anywhere in this function: [3](#0-2) 

The `PendingOrder` storage entry itself does not even retain the message `topic`/outcome, only `nonce`, `block_number`, and `fee`: [4](#0-3) 

This is the local analog of the ZetaChain root cause: the report's bug was that a completion/receipt handler advanced settlement state (marking a CCTX resolved/refunded) using data whose success/nonce binding was never checked against the actual on-chain outcome, letting a party collect twice. Here the queue-processing state machine described in the pallet's own doc comment — "Fetch the pending order by nonce ... pay reward ... remove the order" — never conditions the "pay reward" and "settle/close order" steps on the delivered outcome (`success`) actually being `true`, violating the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically."

### Impact Explanation
Any relayer can submit a real, cryptographically verified `EventProof` for a message whose dispatch on Ethereum reverted (`success = false`) — this is a normal occurrence (e.g. out-of-gas commands, reverted `AgentExecute`/transact calls) and requires no malicious peer/validator/relayer collusion, only an honest relayer submitting genuine failed-dispatch events. The pallet will still:
1. Pay the full relayer fee out of `T::RewardPayment` as if delivery succeeded.
2. Permanently delete the `PendingOrder`, closing off the only on-chain tracking of that nonce.

Because the corresponding assets/fees were already burned/locked on the Substrate side when the message was queued (per `do_process_message` / the v2 send pipeline), a failed dispatch with no success check means: (a) relayers are rewarded for work that did not deliver value to the user, an underpriced/incorrect payout, and (b) the failed message's pending state is destroyed with no compensating unlock or retry path visible in this pallet, i.e. permanent loss of the ability to reconcile that failed transfer. This directly matches the "duplicate settlement or payout" / "permanent user-fund lock" impact categories.

### Likelihood Explanation
High likelihood: `submit_delivery_receipt` is a public, unsigned-permission-only (`ensure_signed`) extrinsic callable by anyone holding a valid Ethereum receipt proof — no privileged role, governance, or malicious-relayer assumption is required, only that some Ethereum-side command reverted (a realistic and expected condition covered by the `success` field's very existence in the ABI event). The existing test suite only exercises `success: true` paths (`mock_valid_event_proof`/`DeliveryReceipt { success: true, .. }` in `test.rs` and the emulated integration tests), so there is no regression coverage catching the missing check.

### Recommendation
In `process_delivery_receipt`, branch on `receipt.success`: only pay `order.fee` when `success == true`; on `false`, still remove/settle the `PendingOrder` (to avoid stuck state) but skip the reward payment, and emit a distinct event (e.g. `MessageDeliveryFailed`) so downstream logic (fee refund to sender, retry, etc.) can react correctly instead of silently rewarding relayers for failed work.

### Proof of Concept
1. A user sends an XCM through `EthereumOutboundQueueV2`; `do_process_message` assigns `nonce = N`, creates `PendingOrders[N] = { nonce: N, fee, block_number }`.
2. The message is delivered to the Ethereum gateway but its embedded command reverts, so the gateway emits `InboundMessageDispatched(nonce = N, topic, success = false, reward_address)`.
3. Any relayer builds a valid `EventProof` for this real event and calls `submit_delivery_receipt(origin, event)`.
4. `T::Verifier::verify` succeeds (real proof), `DeliveryReceipt::try_from` decodes `success = false` correctly.
5. `process_delivery_receipt` executes:
```rust
let order = <PendingOrders<T>>::get(nonce).ok_or(Error::<T>::InvalidPendingNonce)?;
if order.fee > 0 {
    T::RewardPayment::register_reward(&reward_account, T::DefaultRewardKind::get(), order.fee);
}
<PendingOrders<T>>::remove(nonce);
```
`order.fee` is paid and the order removed identically to the `success = true` case — confirmed by the existing tests (`submit_delivery_receipt_succeeds_after_unhalt`, `poc_m1`) which never assert different behavior for `success = false`, and by `invalid_nonce_for_delivery_receipt_fails` showing only the *nonce* lookup is guarded, not the outcome. [5](#0-4)

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

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/types.rs (L14-24)
```rust
/// Pending order
#[derive(Encode, Decode, TypeInfo, Clone, Eq, PartialEq, Debug, MaxEncodedLen)]
pub struct PendingOrder<BlockNumber> {
	/// The nonce used to identify the message
	pub nonce: u64,
	/// The block number in which the message was committed
	pub block_number: BlockNumber,
	/// The fee in Ether provided by the user to incentivize message delivery
	#[codec(compact)]
	pub fee: u128,
}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_outbound.rs (L949-968)
```rust
#[test]
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
