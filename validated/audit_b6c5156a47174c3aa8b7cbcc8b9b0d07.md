### Title
Attacker-controlled `execution_fee` in Snowbridge InboundQueue V2 message is unvalidated, allowing underpriced XCM execution that can stall bridge message processing - (File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs)

### Summary
The reported bug is: a permissioned-but-not-fully-trusted role settles orders using an off-chain-supplied `feeRate` value with no on-chain floor, letting the fee be pushed arbitrarily low (e.g. 1 wei) and bypassing protocol fee guarantees. The closest local analog is `Pallet::process_message` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs`, which accepts a `Message` (decoded from an Ethereum event log emitted by an arbitrary Ethereum contract origin) containing attacker/relayer-influenced `relayer_fee` and `execution_fee` fields, and dispatches/forwards it without enforcing any protocol-defined minimum for `execution_fee`.

### Finding Description
`submit` verifies the Ethereum event proof and decodes it into a `Message` via `Message::try_from(&event.event_log)` [1](#0-0) . This message's fields (`value`, `execution_fee`, `relayer_fee`) are ultimately determined by the Ethereum-side sender when calling `v2_sendMessage` on the Gateway contract, as described in the pallet README [2](#0-1) . `process_message` only validates the gateway address and nonce uniqueness before calling `T::MessageProcessor::process_message`, which converts the message to XCM and forwards it to AssetHub; it does not check that `execution_fee` meets any protocol-level minimum before it is used to pay for XCM execution on the destination chain [3](#0-2) .

The only validated aspects of the relayer-facing incentive are: (a) nonce replay via `NonceBitmap`, and (b) a zero-check on the combined `relayer_fee + tip` before registering a reward, which simply skips reward registration if the total is zero [4](#0-3) . There is no `ensure!(execution_fee >= MinExecutionFee, ...)`-style guard analogous to the recommended `minFeeRate` check from the external report. This mirrors the reported flaw precisely: a value that is supposed to price on-chain work (fee rate / execution fee) is supplied off-chain by an untrusted counterpart and accepted on-chain with only an upper/structural bound (nonce, gateway address) but no floor.

By contrast, the outbound queue in the same bridge computes fees protocol-side via `calculate_fee`/`PricingParameters`, which are governance-set, not user-supplied [5](#0-4) ; inbound-queue-v2 breaks this pattern by letting the remote (Ethereum) side dictate `execution_fee` with no on-chain minimum enforcement.

### Impact Explanation
If `execution_fee` is set arbitrarily low (or zero) by whoever calls `v2_sendMessage` on Ethereum, the resulting XCM forwarded to AssetHub may carry insufficient fees to complete its intended execution (asset deposits, further forwarding, etc.). Underpriced or unpayable XCM instructions can fail mid-execution or get stuck in the XCMP/MessageQueue pipeline, degrading processing of subsequent bridge messages sharing the same queue/lane — a direct match to the "public underpriced work that degrades block production or stalls bridge processing" impact category. This does not require a malicious relayer, validator, or governance actor — merely any unprivileged party controlling the Ethereum-side send call, which is a public entrypoint by design of the bridge.

### Likelihood Explanation
Likelihood is moderate: the Ethereum-side sender is not vetted (`v2_sendMessage` is a public contract call by design), and the relayer submitting the proof is only required to relay a valid, correctly-proven event — they have no incentive or ability to reject an underpriced message once it is emitted on Ethereum. There's no visible protocol-side minimum comparable to `minFeeRate` gating this path.

### Recommendation
Introduce and enforce a protocol-configured minimum (e.g., `MinExecutionFee`/`MinRelayerFee`, settable only by governance) checked in `process_message` before dispatching the message, e.g.:
```rust
ensure!(message.execution_fee >= T::MinExecutionFee::get(), Error::<T>::ExecutionFeeTooLow);
```
This bounds the floor of Ethereum-originated fee/pricing fields the same way the report recommends bounding `feeRate` in the DEX contract, without touching legitimate governance-configured `PricingParameters` used elsewhere in the bridge.

### Proof of Concept
Not independently reproducible from the index alone: I could not confirm from the available files whether the downstream `MessageProcessor`/`MessageToXcm` converter (`bridges/snowbridge/primitives/inbound-queue/src/v2/converter.rs`) or the XCM executor applies any implicit floor on `execution_fee` before dispatch (e.g., a `BuyExecution` weight-based rejection), which would mitigate or negate this issue. The test suite shows `zero_reward_does_not_register_reward` confirms `relayer_fee = 0` is accepted and processed without any fee-floor error [4](#0-3) , and `execution_fee: 1_000_000_000` values are hardcoded in tests rather than derived from any minimum-fee configuration — but the converter's actual use of `execution_fee` (whether it silently swallows underfunded XCM or reverts) was not fully verified given the indexing limits on `converter.rs`. A Devin session with full repo access would be needed to trace `execution_fee` through `MessageToXcm` and confirm whether underpriced XCM causes a hard failure (mitigating impact) or a partial/stuck execution (confirming impact).

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

**File:** bridges/snowbridge/pallets/inbound-queue-v2/README.md (L8-13)
```markdown
**1. Ethereum Gateway Event:** A message is first emitted by a GatewayProxy contract on Ethereum in an OutboundMessageAccepted
event. This event contains:
- A nonce (for replay protection).
- Information about the originating address, asset(s), and XCM payload.
- Relayer fee and execution fee (both in Ether).
This event is emitted when the `v2_registerToken` and `v2_sendMessage` is called on Ethereum.
```

**File:** bridges/snowbridge/pallets/inbound-queue-v2/src/test.rs (L328-354)
```rust
#[test]
fn zero_reward_does_not_register_reward() {
	new_tester().execute_with(|| {
		let relayer: AccountId = Keyring::Bob.into();
		let origin = H160::random();
		assert_ok!(InboundQueue::process_message(
			relayer,
			Message {
				nonce: 0,
				assets: vec![],
				payload: Payload::Raw(vec![]),
				claimer: None,
				execution_fee: 1_000_000_000,
				relayer_fee: 0,
				gateway: GatewayAddress::get(),
				origin,
				value: 3_000_000_000,
			}
		));

		assert_eq!(
			RegisteredRewardsCount::get(),
			0,
			"Zero relayer reward should not be registered"
		);
	});
}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L366-393)
```rust
		/// Calculate total fee in native currency to cover all costs of delivering a message to the
		/// remote destination. See module-level documentation for more details.
		pub(crate) fn calculate_fee(
			gas_used_at_most: u64,
			params: PricingParameters<T::Balance>,
		) -> Fee<T::Balance> {
			// Remote fee in ether
			let fee = Self::calculate_remote_fee(
				gas_used_at_most,
				params.fee_per_gas,
				params.rewards.remote,
			);

			// downcast to u128
			let fee: u128 = fee.try_into().defensive_unwrap_or(u128::MAX);

			// multiply by multiplier and convert to local currency
			let fee = FixedU128::from_inner(fee)
				.saturating_mul(params.multiplier)
				.checked_div(&params.exchange_rate)
				.expect("exchange rate is not zero; qed")
				.into_inner();

			// adjust fixed point to match local currency
			let fee = Self::convert_from_ether_decimals(fee);

			Fee::from((Self::calculate_local_fee(), fee))
		}
```
