## Title
`OutboundQueue::calculate_fee` uses an unguarded `expect()` on `checked_div`, letting message senders panic-halt Snowbridge outbound message processing - (File: `bridges/snowbridge/pallets/outbound-queue/src/lib.rs`)

### Summary
The Venus report's core broken invariant is: a dynamically-computed value is checked against a bound with a hard failure path (`revert`) instead of a saturating/clamping fallback, and because that check sits on the hot path of a function called by almost all critical operations, the failure DoSes the whole subsystem. The local analog is `Pallet::<T>::calculate_fee` in the Snowbridge outbound-queue pallet, which is invoked from `SendMessage::validate` for **every** outbound message (both parachain-originated XCM exports and system-pallet commands) before enqueuing. Instead of returning an error when the fixed-point division fails, it calls `.expect("exchange rate is not zero; qed")`, converting an arithmetic edge case into an unconditional panic. [1](#0-0) 

### Finding Description
`calculate_fee` computes the remote fee in Ether, downcasts it to `u128` (saturating to `u128::MAX` on overflow via `defensive_unwrap_or`), then does:

```
let fee = FixedU128::from_inner(fee)
    .saturating_mul(params.multiplier)
    .checked_div(&params.exchange_rate)
    .expect("exchange rate is not zero; qed")
    .into_inner();
```

`FixedU128::checked_div` returns `None` not only when the divisor is zero, but also whenever the fixed-point division **overflows** the internal `u128` representation (i.e., dividend too large relative to divisor). The dividend here is `fee * params.multiplier`, and `fee` is itself derived from `gas_used_at_most`, which is fully attacker-influenced: it comes from `T::GasMeter::maximum_gas_used_at_most(&message.command)` in `SendMessage::validate`, computed directly from the message a caller submits. [2](#0-1) 

Because the earlier downcast step (`fee.try_into().defensive_unwrap_or(u128::MAX)`) explicitly anticipates and tolerates an overflowed, saturated-to-max `fee` value rather than rejecting it, the code path is designed to continue with `u128::MAX` as a legitimate value. That `u128::MAX`, multiplied by `params.multiplier` (a currently-configured, non-zero, non-adversarial value) and divided by `params.exchange_rate`, can overflow the `FixedU128` internal representation, causing `checked_div` to return `None`. The `.expect(...)` then panics.

This differs from the equivalent VToken bug only in the failure mode: VToken reverts the transaction (contained failure), whereas here the `.expect()` is a hard Rust panic inside pallet logic that executes during `validate()`/message processing — which in a Substrate runtime is unrecoverable within that execution context and can abort block-building/import of the extrinsic or task that triggers it, unlike a contained `Err` return. There is no code path that clamps the value (mirroring VToken's missing `borrowRateMantissa = borrowRateMaxMantissa` clamp) — the only two options are "succeed" or "panic".

`calculate_local_fee`'s sibling function `convert_from_ether_decimals` has the identical anti-pattern: `value.checked_div(denom).expect("divisor is non-zero; qed")`, though `denom` is a `saturating_pow` of a compile-time constant so it is less exposed to attacker input than the `exchange_rate` division. [3](#0-2) 

### Impact Explanation
`calculate_fee` sits on the mandatory path of `SendMessage::validate`, which every outbound message to Ethereum — both user/parachain-originated XCM exports and BridgeHub system commands — must pass through before being enqueued. A panic here during extrinsic execution/message validation aborts that operation path with an unrecoverable Rust panic rather than a graceful `DispatchError`, which is a stronger and less-controlled failure mode than the reverts studied in the reference report, and directly maps to "public underpriced work that degrades block production or stalls bridge processing" in the required impact set: any account able to submit a message with a sufficiently large ABI-encoded command (bounded only by `MaxMessagePayloadSize`, not by economic cost that scales with the resulting fee-overflow risk) can attempt to drive the fee computation into the overflow branch.

### Likelihood Explanation
Triggering the panic requires the currently governance-configured `PricingParameters` (multiplier, exchange rate) to be in a range where a maximal attacker-supplied `gas_used_at_most`/payload pushes the intermediate `FixedU128` product past its representable range on division — this is data-dependent and not automatically true for all parameter configurations, so likelihood is conditional on current pricing parameters rather than universally exploitable at all times. This is analogous to the Venus finding being rated Medium (disputed) rather than Critical: the hard-coded/parameter-dependent boundary is real and reachable through normal dynamic inputs, but requires specific market/parameter conditions to manifest, precisely mirroring the disputed-severity reasoning in the original report.

### Recommendation
Replace the `.expect(...)` calls in `calculate_fee` and `convert_from_ether_decimals` with saturating fallbacks or a proper `Err`/`SendError` return (e.g., reject the message with `SendError::Fee`/similar instead of panicking), so that an unfavorable but legitimate parameter/gas combination degrades gracefully (reject that one message) rather than panicking the pallet's message-validation code path.

### Proof of Concept
1. Governance sets `PricingParameters` with a `multiplier` and `exchange_rate` combination that is valid under normal expected message sizes (no malicious governance action needed — this is a reasonable, non-adversarial configuration).
2. An unprivileged user (or a sibling parachain) submits an outbound message via XCM export whose `command` ABI-encodes to a payload up to `MaxMessagePayloadSize`, chosen so `T::GasMeter::maximum_gas_used_at_most` returns a very large `u64` gas estimate.
3. `SendMessage::validate` calls `calculate_fee(gas_used_at_most, params)`; `calculate_remote_fee` computes `fee_per_gas * gas_used_at_most + reward` which, downcast and multiplied by `multiplier`, produces a value whose division by `exchange_rate` overflows `FixedU128`'s internal `u128`.
4. `checked_div` returns `None`; `.expect("exchange rate is not zero; qed")` panics, aborting the validation of that message (and, depending on execution context, the encompassing extrinsic/task), rather than returning a graceful error to the caller. [4](#0-3)

### Citations

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

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L404-418)
```rust
		/// The local component of the message processing fees in native currency
		pub(crate) fn calculate_local_fee() -> T::Balance {
			T::WeightToFee::weight_to_fee(
				&T::WeightInfo::do_process_message().saturating_add(T::WeightInfo::commit_single()),
			)
		}

		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-60)
```rust
	fn validate(
		message: &Message,
	) -> Result<(Self::Ticket, Fee<<Self as SendMessageFeeProvider>::Balance>), SendError> {
		// The inner payload should not be too large
		let payload = message.command.abi_encode();
		ensure!(
			payload.len() < T::MaxMessagePayloadSize::get() as usize,
			SendError::MessageTooLarge
		);

		// Ensure there is a registered channel we can transmit this message on
		ensure!(T::Channels::contains(&message.channel_id), SendError::InvalidChannel);

		// Generate a unique message id unless one is provided
		let message_id: H256 = message
			.id
			.unwrap_or_else(|| unique((message.channel_id, &message.command)).into());

		let gas_used_at_most = T::GasMeter::maximum_gas_used_at_most(&message.command);
		let fee = Self::calculate_fee(gas_used_at_most, T::PricingParameters::get());
```
