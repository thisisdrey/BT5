This confirms the analog: `send_xcm` in `bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs` only validates and hands the XCM off to `Sender::deliver(ticket)` — a purely local enqueue into the outbound HRMP/XCMP channel. It never waits for, or gets confirmation of, actual execution of the XCM on the destination (AssetHub). Actual execution of the `remote_xcm` (asset deposit to beneficiary) happens later, asynchronously, in a different block, by the destination chain's own `MessageQueue`/`XcmExecutor`, and can fail (`Outcome::Incomplete`/`Outcome::Error`), trapping the assets.

### Title
Inbound bridge nonce is marked processed and relayer reward paid before the bridged asset transfer actually executes/settles - ([File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs])

### Summary
`Pallet::process_message` marks the Ethereum message `nonce` as consumed and pays the relayer's reward as soon as the converted XCM is *handed to the outbound router*, not after the XCM has actually executed and the funds have settled at the beneficiary on AssetHub. This mirrors the CDPVault bug class: a partial/incomplete action (enqueue) is treated as full resolution (nonce "safe"/settled), which forecloses any future corrective processing of that nonce even if the real settlement later fails.

### Finding Description
In `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`: [1](#0-0) 

`Nonce::<T>::set(nonce)` (permanent replay marker) is written, then `T::MessageProcessor::process_message` is invoked, and if it returns `Ok`, the relayer fee/tip is immediately paid via `T::RewardPayment::register_reward`.

The `XcmMessageProcessor::process_xcm` implementation only converts the message and calls `send_xcm`, which itself only validates and hands the ticket to `Sender::deliver(ticket)`: [2](#0-1) 

`Sender::deliver` (the XCMP/HRMP router) merely enqueues the message into the local outbound channel for the destination parachain; it returns `Ok` as soon as the enqueue succeeds. The actual `remote_xcm` execution (the asset deposit instructions targeting the `claimer`/beneficiary) happens later, in a separate block, on AssetHub's own `MessageQueue`/`XcmExecutor` (see `ProcessXcmMessage::process_message`), which can return `Outcome::Incomplete` or `Outcome::Error` and simply drop/trap the assets: [3](#0-2) 

Because `process_message` in the inbound-queue-v2 pallet already committed `Nonce::<T>::set(nonce)` and paid the relayer before this remote execution occurs, there is no atomic link between "nonce consumed / reward paid" and "beneficiary actually received funds." The `ensure!(!Nonce::<T>::get(nonce), Error::<T>::InvalidNonce)` guard (line 222) then permanently blocks any resubmission of that nonce, even though the corresponding transfer may never have completed successfully on the destination chain.

### Impact Explanation
This falls under the "Message queues, bridge markers, receipts, and payout state must only advance after decode, dispatch, execution, and settlement succeed atomically" pivot. If the remote XCM execution fails (e.g., insufficient weight budget on AssetHub, a barrier/filter rejection, or an asset-location conversion edge case not caught by `ConvertMessage`), the bridged funds are trapped/lost from the user's perspective while:
1. The `nonce` is permanently marked as consumed, so the same Ethereum-side event can never be resubmitted/retried through `submit`.
2. The relayer has already been paid `relayer_fee + tip` for a delivery that did not actually settle.

This is a "permanent user-fund lock" / duplicate-settlement class issue: the queue's completion marker advances on the wrong condition.

### Likelihood Explanation
This does not require a malicious relayer, validator, or governance actor — it is triggered purely by conditions on the destination chain's message processing (weight limits, execution outcome) that are outside the inbound-queue-v2 pallet's control, combined with the pallet's own design of settling nonce/reward on enqueue rather than on confirmed execution. The `MessageId`/`claimer` asset-trap mechanism exists as a partial mitigation, but it depends on a separate manual claim flow and does not retroactively reconcile the nonce or reward-payout state, so it does not fully close the gap identified here.

### Recommendation
Do not treat `Sender::deliver` success as final settlement. Either:
- Defer `Nonce::<T>::set(nonce)` and reward payment until receiving confirmation that the destination XCM actually executed successfully (e.g., via a receipt/ack mechanism back from AssetHub), or
- Explicitly document and bound the impact by ensuring the asset-trap/claimer path is unconditionally sufficient to make the original beneficiary whole for every possible `Outcome::Incomplete`/`Outcome::Error` path, and decouple relayer reward payment from `process_message` success, instead conditioning it on confirmed remote execution.

### Proof of Concept
This requires cross-chain execution differences (Bridge Hub inbound-queue-v2 enqueuing to AssetHub) that cannot be fully reproduced in a single-pallet unit test; the local unit tests in `bridges/snowbridge/pallets/inbound-queue-v2/src/test.rs` only assert that `process_message` succeeds and pays out rewards once `MockXcmSender`/`MockXcmExecutor` return `Ok` — they do not model a scenario where the destination `XcmExecutor::execute` later returns `Outcome::Incomplete`/`Outcome::Error`, which is exactly the gap described above: [4](#0-3) 
Full end-to-end reproduction would need a Devin session running the Snowbridge/BridgeHub-AssetHub emulated integration test setup (`cumulus/parachains/integration-tests/emulated/tests/bridges/...`) to force an `Outcome::Incomplete` on AssetHub for the forwarded `remote_xcm` and observe that the nonce is still consumed and the relayer still rewarded.

### Citations

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

**File:** polkadot/xcm/xcm-builder/src/process_xcm_message.rs (L91-127)
```rust
		let (consumed, result) = match XcmExecutor::execute(origin.into(), pre, id, Weight::zero())
		{
			Outcome::Complete { used } => {
				tracing::trace!(
					target: LOG_TARGET,
					"XCM message execution complete, used weight: {used}",
				);
				(used, Ok(true))
			},
			Outcome::Incomplete { used, error: InstructionError { index, error } } => {
				tracing::trace!(
					target: LOG_TARGET,
					?error,
					?index,
					?used,
					"XCM message execution incomplete",
				);
				(used, Ok(false))
			},
			// In the error-case we assume the worst case and consume all possible weight.
			Outcome::Error(InstructionError { error, index }) => {
				tracing::trace!(
					target: LOG_TARGET,
					?error,
					?index,
					"XCM message execution error",
				);
				let error = match error {
					xcm::latest::Error::ExceedsStackLimit => ProcessMessageError::StackLimitReached,
					_ => ProcessMessageError::Unsupported,
				};

				(required, Err(error))
			},
		};
		meter.consume(consumed);
		result
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/test.rs (L394-439)
```rust
#[test]
fn inbound_tip_is_paid_out_to_relayer() {
	new_tester().execute_with(|| {
		let nonce: u64 = 77;
		let tip: u128 = 12_345;
		let relayer_fee: u128 = 2_000;

		// Add tip for nonce before message is processed
		assert_ok!(InboundQueue::add_tip(nonce, tip));
		assert_eq!(Tips::<Test>::get(nonce), Some(tip));

		// Process inbound message with relayer_fee
		let relayer: AccountId = Keyring::Bob.into();
		assert_ok!(InboundQueue::process_message(
			relayer,
			Message {
				nonce,
				assets: vec![],
				payload: Payload::Raw(vec![]),
				claimer: None,
				execution_fee: 1_000_000_000,
				relayer_fee,
				gateway: mock::GatewayAddress::get(),
				origin: H160::random(),
				value: 3_000_000_000,
			},
		));

		// Reward should be registered from relayer_fee + tip
		assert_eq!(
			RegisteredRewardsCount::get(),
			1,
			"Reward should be registered from relayer_fee + tip"
		);

		// Check the actual reward amount paid out (should be relayer_fee + tip)
		assert_eq!(
			RegisteredRewardAmount::get(),
			relayer_fee + tip,
			"Reward amount should equal relayer_fee + tip"
		);

		// Tip should be consumed from storage
		assert_eq!(Tips::<Test>::get(nonce), None);
	});
}
```
