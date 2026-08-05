This confirms the claim precisely. The test at `cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs` lines 467-496 demonstrates exactly the split described: `EthereumInboundQueueV2::process_message` succeeds and fires `RewardRegistered` for the full `relayer_reward` on BridgeHub, while the corresponding XCM execution on AssetHub subsequently fails (`pallet_message_queue::Event::Processed{success: false}`) because the asset was not yet created before `ReserveAssetDeposited` is attempted.

The root cause in `process_message` at `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` lines 214-245 confirms the reward registration (`T::RewardPayment::register_reward`) and nonce consumption (`Nonce::<T>::set(nonce)`) both occur based solely on the return of `T::MessageProcessor::process_message`, which per `bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs` lines 86-109 only guarantees successful local XCMP enqueue via `Sender::deliver(ticket)` — not destination execution success. There is no correlation/reversal path tied to `pallet_message_queue::Event::Processed{success:false}` on AssetHub.

Audit Report

## Title
Inbound Queue V2 registers relayer reward and marks nonce settled before destination XCM execution/settlement is confirmed - (File: `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`)

## Summary
`Pallet::process_message` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` marks the nonce processed and calls `T::RewardPayment::register_reward` for the relayer as soon as `T::MessageProcessor::process_message` (the `XcmMessageProcessor`) succeeds — which only means the derived XCM was successfully placed into the local outbound XCMP queue via `Sender::deliver(ticket)`, not that it executed/settled on AssetHub. Destination execution happens later, asynchronously, via `pallet_message_queue`, and can fail independently with no reversal of the reward or nonce state.

## Finding Description
`submit` verifies the Ethereum proof and decodes the message, then calls `process_message` [1](#0-0) . Inside `process_message`, the nonce is set as consumed, the message is handed to `MessageProcessor::process_message`, and — contingent only on that call's success — the relayer reward is registered and `MessageReceived` is emitted [2](#0-1) . The `XcmMessageProcessor::send_xcm` implementation shows that "success" here means only `validate_send` + `Sender::deliver(ticket)` succeeded, i.e., local enqueue into the outbound XCMP channel, not remote execution [3](#0-2) . There is no code path in this pallet, or correlated with `pallet_message_queue`'s processing outcome on AssetHub, that reverts the nonce or reward if destination execution fails.

## Impact Explanation
This is duplicate/unbacked settlement relative to actual work: the relayer receives full `relayer_fee` reward and the nonce is permanently consumed as "processed" even when the funds never settle to the intended beneficiary on AssetHub (asset creation/deposit failure, barrier rejection, insufficient weight, etc.). This matches the required invariant that "message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically," and constitutes public underpriced work / duplicate-settlement-style value leakage from the bridge.

## Likelihood Explanation
Any relayer submitting a validly-proven Ethereum event can trigger this: verification only checks proof-of-origin/nonce validity, not eventual destination execution outcome. The existing integration test in this very repository reproduces the exact split, using an XCM payload referencing an unregistered asset, showing this is trivially reachable without privileged access, malicious node/validator assumptions, or key compromise.

## Recommendation
Do not finalize `T::RewardPayment::register_reward` (and ideally the "processed" nonce semantics used for reward purposes) until destination settlement is confirmed. Options: defer reward registration until an explicit settlement acknowledgment from AssetHub is received (analogous to bridge-messages delivery-proof flows), or make the reward conditional/refundable/clawed-back if the enqueued XCM's `pallet_message_queue::Event::Processed{success:false}` is observed for the corresponding `message_id`.

## Proof of Concept
The existing integration test demonstrates this exactly:
1. `EthereumInboundQueueV2::process_message` is called on BridgeHub with a payload whose XCM tries `ReserveAssetDeposited`/`DepositAsset` for an asset not yet created on AssetHub [4](#0-3) .
2. On BridgeHub, `pallet_bridge_relayers::Event::RewardRegistered` fires for the full `relayer_reward`, and the nonce is consumed [5](#0-4) .
3. On AssetHub, `pallet_message_queue::Event::Processed{success: false, ..}` fires — the deposit never settles [6](#0-5) .
4. Result: the relayer reward is paid and the message permanently marked processed, despite the funds never being credited to the intended beneficiary on AssetHub, with no mechanism to reverse either.

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

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L455-467)
```rust
		let message = Message {
			gateway: origin,
			nonce: 1,
			origin,
			assets,
			payload: Payload::Raw(versioned_message_xcm.encode()),
			claimer: Some(claimer_bytes),
			value: 3_500_000_000_000u128,
			execution_fee: 1_500_000_000_000u128,
			relayer_fee: relayer_reward,
		};

		EthereumInboundQueueV2::process_message(relayer_account.clone(), message).unwrap();
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L469-480)
```rust
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
```

**File:** cumulus/parachains/integration-tests/emulated/tests/bridges/bridge-hub-westend/src/tests/snowbridge_v2_inbound.rs (L483-496)
```rust
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
```
