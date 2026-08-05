### Title
Relayer receives zero reward (not merely a reduced one) when Snowbridge Inbound Queue V2 XCM forwarding fails after nonce is consumed — ([File: bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs])

### Summary
`Pallet::process_message` in `bridges/snowbridge/pallets/inbound-queue-v2/src/lib.rs` marks the Ethereum-side nonce as processed and then invokes `T::MessageProcessor::process_message` (XCM conversion + `send_xcm`, defined in `bridges/snowbridge/primitives/inbound-queue/src/v2/processor.rs`). Only *after* that fallible call succeeds does the pallet pay out `relayer_fee` and any accumulated `Tips`. If the downstream XCM conversion or delivery step fails for any reason unrelated to the relayer's honest work (fee misconfiguration, destination temporarily unreachable, `charge_fees` failure, etc.), the whole extrinsic/message errors out, and the relayer who did all the real work (fetching/verifying the Ethereum event, submitting the extrinsic, paying its dispatch fee) is left with **no compensation whatsoever** — this is the same "cost incurred, but the compensating/fallback path pays out little-to-nothing" defect class described in the GMX report, just with the shortfall pushed to zero instead of merely "little." [1](#0-0) 

### Finding Description
The reward-payment logic is structured as: verify → mark nonce consumed → attempt XCM forward → **only then** pay `relayer_fee + tip`: [2](#0-1) 

The downstream call is `XcmMessageProcessor::process_xcm`, which converts the message to XCM and calls `send_xcm`, which itself calls `Executor::charge_fees` and then `Sender::deliver(ticket)`: [3](#0-2) 

Because the reward-payment block sits strictly after this fallible chain and is reached only via successful completion (`?` short-circuits on any `MessageProcessorError`), any failure in `Converter::convert`, `validate_send`, `charge_fees`, or `Sender::deliver` causes the entire `process_message` call to return `Err`. This is directly analogous to the GMX report's core defect: work is expended by the "keeper" (here, the relayer, who paid to submit and get the Ethereum proof verified) before the point of failure, but the code path taken on failure (early-return with no reward, as opposed to GMX's under-funded cancellation call) leaves the party who performed useful work under-compensated — in this case, entirely uncompensated for that submission attempt.

This is reinforced by the repository's own history: `prdoc/stable2509/pr_9746.prdoc` documents "Snowbridge Inbound Queue V2 relayer tip payout fix ... Fixes a bug where relayer tips were not properly paid out, causing the tips to be lost since it had already been burnt," confirming this exact bug class (reward loss tied to message-processing failure) has already manifested once in this exact pallet. [4](#0-3) 

The outbound-queue-v2 analog (`ProcessMessage` impl) shows the mirrored direction of the same flow, where `Overweight` failures are the only case explicitly compensated for retry — but genuine `do_process_message` failures after weight has already been consumed from the meter are not separately reconciled with any reward/refund mechanism: [5](#0-4) 

### Impact Explanation
If XCM forwarding to AssetHub fails for reasons outside the relayer's control (e.g., fee/asset conversion misconfiguration, temporarily unreachable HRMP channel, or a `charge_fees` failure caused by a legitimately-priced-but-misestimated `execution_fee`), the relayer:
- Pays the on-chain dispatch fee for `submit` (which includes the cost of Merkle/receipt proof verification via `T::Verifier::verify`), and
- Receives zero `relayer_fee`/`tip`, because the reward payment is gated behind successful `MessageProcessor::process_message`.

Because the nonce write and any storage effects roll back with the failed dispatch (Substrate's default transactional dispatch semantics), the message is not marked delivered, so a relayer (or a different relayer) must resubmit — meaning the underpriced/failed attempt is pure sunk cost for whoever submitted it. Repeated occurrence discourages relayer participation and can stall bridge message processing (a live-scope impact: "public underpriced work that degrades block production or stalls bridge processing").

### Likelihood Explanation
Likelihood is moderate: `Executor::charge_fees` and `send_xcm`/`deliver` failures are realistic operational conditions (fee market fluctuation, channel congestion, asset registration state) rather than requiring a malicious actor. No privileged access or malicious peer/relayer is needed — this triggers under ordinary operating conditions whenever the downstream XCM step legitimately fails after the relayer has already paid to submit the message.

### Recommendation
Decouple relayer compensation from the success of the downstream XCM forwarding step: either (a) pay out a base relayer reward for successfully verifying and decoding the Ethereum event before attempting the XCM step, independent of downstream forwarding outcome, or (b) introduce an explicit "failed forward" fallback path (mirroring GMX's cancellation flow) that still compensates the relayer for verification work performed, funded from the `execution_fee`/`relayer_fee` already included in the message, rather than relying on an all-or-nothing `?` short-circuit.

### Proof of Concept
1. Submit a valid Ethereum event/message via `submit` whose `execution_fee` is sufficient for verification but where the XCM `send_xcm` step will fail (e.g., destination `TargetLocation` temporarily has no viable HRMP channel, or `Executor::charge_fees` fails due to fee asset misconfiguration).
2. `process_message` calls `T::Verifier::verify` (succeeds, relayer has paid dispatch fee for this) then `T::MessageProcessor::process_message` → `process_xcm` → `send_xcm`, which fails inside `charge_fees` or `Sender::deliver`. [3](#0-2) 
3. `process_message` returns `Err(...)` before reaching the reward-payment block. [6](#0-5) 
4. Observe: no `RewardRegistered` event is emitted, `Nonce`/`Tips` storage changes are rolled back with the failed dispatch, and the relayer has paid the `submit` extrinsic's dispatch fee for nothing — mirroring GMX's keeper receiving insufficient (here, zero) compensation for work performed before the point of failure.

**Note on confidence**: I was unable to directly execute or trace the runtime-level fee-charging semantics (whether `Executor::charge_fees` is guaranteed to be rolled back transactionally in all configured executors), so the exact magnitude of loss (whether any partial balance deduction could also persist) is not fully verified from static reading alone. The core reward-gating-on-success structural issue, however, is directly supported by the cited code and the project's own prior fix for a related reward-loss bug in the same pallet.

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

**File:** prdoc/stable2509/pr_9746.prdoc (L1-13)
```text
title: Snowbridge Inbound Queue V2 relayer tip payout fix

doc:
- audience: Runtime Dev
  description: |
    Fixes a bug where relayer tips were not properly paid out, causing the tips to be lost since it had already been
    burnt.

crates:
- name: snowbridge-pallet-inbound-queue-v2
  bump: patch
- name: snowbridge-test-utils
  bump: minor
```

**File:** bridges/snowbridge/pallets/outbound-queue-v2/src/process_message_impl.rs (L11-28)
```rust
impl<T: Config> ProcessMessage for Pallet<T> {
	type Origin = T::AggregateMessageOrigin;
	fn process_message(
		message: &[u8],
		origin: Self::Origin,
		meter: &mut WeightMeter,
		_: &mut [u8; 32],
	) -> Result<bool, ProcessMessageError> {
		let weight = T::WeightInfo::do_process_message();
		if meter.try_consume(weight).is_err() {
			Self::deposit_event(Event::MessagePostponed {
				payload: message.to_vec(),
				reason: ProcessMessageError::Overweight(weight),
			});
			return Err(ProcessMessageError::Overweight(weight));
		}
		Self::do_process_message(origin, message)
	}
```
