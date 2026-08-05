## Analysis



This is a legitimate architectural analog: the reward/settlement state in the Snowbridge V2 inbound pipeline advances based on successful *enqueueing* of the cross-chain message rather than on confirmed *execution/settlement* at the destination, mirroring the Derby report's "acting on a feedback signal before the actual fund transfer completes."

### Title
Inbound Queue V2 registers relayer reward and marks nonce settled before destination XCM execution/settlement is confirmed - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

### Summary
`Pallet::process_message` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` decodes an Ethereum-origin message, hands it to `T::MessageProcessor::process_message` (the `XcmMessageProcessor` in `bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs`), and treats a successful `Sender::deliver(ticket)` call — i.e., successful *enqueueing* of the derived XCM into the local outbound XCMP queue — as proof that the cross-chain asset transfer to AssetHub has succeeded. Immediately afterwards it marks the nonce as processed and calls `T::RewardPayment::register_reward(...)` to pay the relayer, and emits `Event::MessageReceived`. None of this is contingent on the XCM actually being dispatched and the assets actually settling on AssetHub, which happens later, asynchronously, via `pallet_message_queue`. [1](#0-0) 

### Finding Description
The `submit` extrinsic verifies the Ethereum proof, decodes the `Message`, and calls `process_message`: [2](#0-1) 

Inside `process_message`, the nonce is marked used and the message is handed to `MessageProcessor::process_message`, whose only job (via `XcmMessageProcessor::process_xcm` / `send_xcm`) is to convert the message to XCM and call `Sender::deliver(ticket)` — this just places the message in the local outbound queue destined for AssetHub: [3](#0-2) 

As soon as that local enqueue succeeds, `process_message` treats the operation as done: it registers the relayer's Ether reward and emits `MessageReceived`: [4](#0-3) 

The actual dispatch and execution of that XCM (asset creation, `ReserveAssetDeposited`, `DepositAsset`) happens later and asynchronously on AssetHub through `pallet_message_queue`, and can fail independently — e.g., if the referenced asset has not yet been registered, or execution runs out of weight/fees. There is no mechanism that reverts the nonce-consumption or reward-registration on BridgeHub if the downstream execution on AssetHub fails or if the assets end up trapped. This is the same "receive-before-arrival" pattern as the Derby report: a `receiveFunds()`-equivalent state transition (nonce consumed, reward paid, `MessageReceived` emitted) fires on the strength of a *dispatch* signal, not a *settlement confirmation* signal, while the actual value transfer travels a separate, independently-timed path (queued XCMP → `MessageQueue` execution on AssetHub).

### Impact Explanation
This directly maps onto the required pivot that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically." Here, the reward payout and nonce/settlement bookkeeping advance purely on local dispatch success, decoupled from destination-chain settlement. Concretely, when AssetHub execution fails (asset missing, barrier rejection, insufficient weight, etc.), the relayer has already been irrevocably rewarded and the nonce irrevocably consumed for a transfer whose funds did not settle to the intended beneficiary — an unbacked/duplicate-style payout relative to actual settled work, and a source of protocol value leakage over repeated relaying of messages engineered to fail on AssetHub while still claiming full relayer_fee on BridgeHub.

### Likelihood Explanation
Any user or the relayer itself can construct/trigger a Gateway `OutboundMessageAccepted` event whose XCM payload is guaranteed or likely to fail at the destination (e.g., referencing an unregistered `token_location`/asset, as exercised by the existing test) while still paying the relayer_fee out of the message's locked Ether. Because verification only checks proof-of-origin/nonce validity — not eventual execution outcome — this path is reachable by any relayer without needing a malicious node, validator, or governance actor, satisfying the "public underpriced work" / "duplicate settlement" impact class.

### Recommendation
Do not finalize reward registration (and ideally do not treat the nonce as "settled" for reward purposes) until destination execution/settlement is confirmed. Options: (a) defer relayer reward registration until AssetHub reports back settlement success (e.g., via a settlement/ack channel similar to `receive_messages_delivery_proof` in the bridge-messages pallet), or (b) make the reward conditional/refundable if the enqueued XCM fails on the destination (tracked via `pallet_message_queue::Event::Processed{success:false}` correlated by `message_id`).

### Proof of Concept
The existing integration test already reproduces the exact split between "reward registered" and "funds not settled": [5](#0-4) 

1. Call `EthereumInboundQueueV2::process_message` on BridgeHub with a payload that references an asset that has not been created on AssetHub (`SetReservesCallIndex`/`CreateAssetCallIndex` transacts inside the same XCM that also tries `ReserveAssetDeposited`).
2. Observe on BridgeHub that `pallet_bridge_relayers::Event::RewardRegistered` fires for the full `relayer_fee`, and the nonce is consumed — this happens purely from the successful `Sender::deliver` into the XCMP outbound queue.
3. Observe on AssetHub that `pallet_message_queue::Event::Processed{ success: false, .. }` fires — the underlying asset deposit never settles.
4. Result: relayer reward is paid and the message is permanently marked processed, despite the funds never being credited to the intended beneficiary on AssetHub.

### Citations

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L185-198)
```rust
		pub fn submit(origin: OriginFor<T>, event: Box<EventProof>) -> DispatchResult {
			let who = ensure_signed(origin)?;
			ensure!(!OperatingMode::<T>::get().is_halted(), Error::<T>::Halted);

			// submit message for verification
			T::Verifier::verify(&event.event_log, &event.proof)
				.map_err(|e| Error::<T>::Verification(e))?;

			// Decode event log into a bridge message
			let message =
				Message::try_from(&event.event_log).map_err(|_| Error::<T>::InvalidMessage)?;

			Self::process_message(who, message)
		}
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs (L214-245)
```rust
	impl<T: Config> Pallet<T> {
		pub fn process_message(relayer: T::AccountId, message: Message) -> DispatchResult {
			// Verify that the message was submitted from the known Gateway contract
			ensure!(T::GatewayAddress::get() == message.gateway, Error::<T>::InvalidGateway);

			let (nonce, relayer_fee) = (message.nonce, message.relayer_fee);

			// Verify the message has not been processed
			ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce);

			// Mark message as received
			Nonce::<T>::set(nonce);

			let message_id = T::MessageProcessor::process_message(relayer.clone(), message)
				.map_err(|e| match e {
					MessageProcessorError::ProcessMessage(e) => e,
					MessageProcessorError::ConvertMessage(e) => Error::<T>::from(e).into(),
					MessageProcessorError::SendMessage(e) => Error::<T>::from(e).into(),
				})?;

			// Pay relayer reward
			let tip = Tips::<T>::take(nonce).unwrap_or_default();
			let total_tip = relayer_fee.saturating_add(tip);
			if total_tip > 0 {
				T::RewardPayment::register_reward(&relayer, T::DefaultRewardKind::get(), total_tip);
			}

			// Emit event with the message_id
			Self::deposit_event(Event::MessageReceived { nonce, message_id });

			Ok(())
		}
```

**File:** bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs (L86-109)
```rust
	fn send_xcm(
		dest: Location,
		fee_payer: &T::AccountId,
		xcm: Xcm<()>,
	) -> Result<XcmHash, SendError> {
		let fee_payer = AccountToLocation::try_convert(fee_payer).map_err(|err| {
			tracing::error!(
				target: LOG_TARGET,
				?err,
				"Failed to convert account to XCM location",
			);
			SendError::NotApplicable
		})?;
		let (ticket, fee) = validate_send::<Sender>(dest, xcm)?;
		Executor::charge_fees(fee_payer, fee).map_err(|error| {
			tracing::error!(
				target: LOG_TARGET,
				?error,
				"Charging fees failed with error",
			);
			SendError::Fees
		})?;
		Sender::deliver(ticket)
	}
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L467-497)
```rust
		EthereumInboundQueueV2::process_message(relayer_account.clone(), message).unwrap();

		assert_expected_events!(
			BridgeHubWestend,
			vec![
				RuntimeEvent::XcmpQueue(cumulus_pallet_xcmp_queue::Event::XcmpMessageSent { .. }) => {},
				// Check that the relayer reward was registered.
				RuntimeEvent::BridgeRelayers(pallet_bridge_relayers::Event::RewardRegistered { relayer, reward_kind, reward_balance }) => {
					relayer: *relayer == relayer_account,
					reward_kind: *reward_kind == BridgeReward::Snowbridge,
					reward_balance: *reward_balance == relayer_reward,
				},
			]
		);
	});

	AssetHubWestend::execute_with(|| {
		type RuntimeEvent = <AssetHubWestend as Chain>::RuntimeEvent;

		assert_expected_events!(
			AssetHubWestend,
			vec![
				// message should not be processed, since assets cannot be ReserveAssetDeposited
				// before the asset has been created.
				RuntimeEvent::MessageQueue(
					pallet_message_queue::Event::Processed { success: false, .. }
				) => {},
			]
		);
	});
}
```
