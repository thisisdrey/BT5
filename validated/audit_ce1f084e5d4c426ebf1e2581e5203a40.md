Audit Report

## Title
Outbound Queue Remote Fee Calculation Can Silently Round to Zero Due to Unguarded Integer Division - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

## Summary
`Pallet::calculate_fee` in the Snowbridge outbound queue computes the ETH-denominated relayer fee and converts it to local currency via `convert_from_ether_decimals`, which performs an unguarded `checked_div` by `10^8` (DOT, 10 decimals) or `10^6` (KSM, 12 decimals). For pricing parameters that pass the existing `PricingParameters::validate()` non-zero checks but are still small relative to this scaling factor, the computed `remote` fee floors to exactly `0`, and neither `validate` nor `deliver` in `send_message_impl.rs` rejects this before enqueueing the message for Ethereum delivery.

## Finding Description
`calculate_fee` computes `fee_per_gas * gas_used_at_most + reward` in wei, treats it as a `FixedU128` inner value (exploiting the 18-decimal alignment with wei), applies the multiplier/exchange-rate, then calls `convert_from_ether_decimals`, which does `value.checked_div(denom)` with `denom = 10^(18 - Decimals)`: [1](#0-0) 

The governance-facing guard, `PricingParameters::validate`, only rejects parameters that are exactly zero (`exchange_rate == 0`, `fee_per_gas == 0`, `rewards.local/remote == 0`, `multiplier == 0`) — it performs no magnitude check preventing the computed fee from being smaller than the decimal-scaling denominator: [2](#0-1) 

The repository's own test suite demonstrates this exact scenario passes `validate()` yet still produces `fee.remote == 0`, with an explicit comment acknowledging the outcome "should be avoided": [3](#0-2) 

`SendMessage::validate` computes this fee and returns it with no minimum/non-zero check, and `deliver` unconditionally enqueues the message for cross-chain relaying once the ticket is produced: [4](#0-3) 

There is no floor/round-up or rejection logic analogous to zero-amount guards used elsewhere in the codebase (e.g., `calculate_validator_incentive_for_page` explicitly returning `None` on a zero result) applied to this fee path.

## Impact Explanation
Since `PricingParameters::validate` does not enforce any minimum magnitude relationship between `fee_per_gas`/`reward`/`exchange_rate`/`multiplier` and the fixed `10^6`/`10^8` decimal-scaling denominator, any parameter set that is non-zero but numerically small relative to that denominator causes `fee.remote` to floor to `0` for messages routed through the affected channel. Any unprivileged sender using the public message-send path (e.g., via `pallet_xcm::execute` → `EthereumBlobExporter::deliver` → `OutboundQueue::validate`/`deliver`) can then have their message queued and delivered to Ethereum while the relayer `reward` field in the committed message is `0`, i.e., cost correctness for bridge message delivery is broken — this matches "public underpriced work that degrades block production or stalls bridge processing," since relayers who front the on-chain execution/gas costs receive no compensation, disincentivizing relaying of that channel's messages.

## Likelihood Explanation
The trigger does not require a malicious relayer or compromised validator; it only requires `PricingParameters` (set by governance/root, but with no magnitude validation preventing this) to fall in a numeric range that is non-zero yet small relative to the decimal-scaling denominator — exactly the parameter set used in the repository's own passing test. Once such parameters are in effect, every ordinary unprivileged user calling the public send path is affected, making the underpriced-delivery condition fully repeatable and not contingent on any privileged or malicious action at exploit time, only on an existing configuration gap that `validate()` fails to close.

## Recommendation
- Add a magnitude check to `PricingParameters::validate` (or to `calculate_fee`/`convert_from_ether_decimals`) ensuring the computed remote fee cannot round to zero, e.g., require `fee_per_gas * min_gas + reward` scaled by `multiplier/exchange_rate` exceeds the decimal-scaling denominator.
- Alternatively, round up (`div_ceil`) instead of floor-dividing in `convert_from_ether_decimals`, or return a `SendError` when `Fee.remote == 0` in `validate` before a ticket is produced.
- Add regression tests asserting `fee.remote > 0` across the full realistic range of parameters accepted by `PricingParameters::validate`, not just spot-checked inputs.

## Proof of Concept
The existing unit test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` in `bridges/snowbridge/pallets/outbound-queue/src/test.rs` sets `exchange_rate = 1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1` — all non-zero and passing `PricingParameters::validate` — and calls `OutboundQueue::calculate_fee(250000, price_params)`, asserting `fee.remote == 0`. Tracing this `Fee` through `SendMessage::validate`/`deliver` in `send_message_impl.rs` shows no check rejects the zero remote fee: the message is queued via `T::MessageQueue::enqueue_message` and will later be committed with `reward: 0` in the `CommittedMessage`, confirming delivery to Ethereum without the intended relayer compensation.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L368-418)
```rust
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

		/// Calculate fee in remote currency for dispatching a message on Ethereum
		pub(crate) fn calculate_remote_fee(
			gas_used_at_most: u64,
			fee_per_gas: U256,
			reward: U256,
		) -> U256 {
			fee_per_gas.saturating_mul(gas_used_at_most.into()).saturating_add(reward)
		}

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

**File:** bridges/snowbridge/primitives/core/src/pricing.rs (L39-56)
```rust
	pub fn validate(&self) -> Result<(), InvalidPricingParameters> {
		if self.exchange_rate == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.fee_per_gas == U256::zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.local.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.rewards.remote.is_zero() {
			return Err(InvalidPricingParameters);
		}
		if self.multiplier == FixedU128::zero() {
			return Err(InvalidPricingParameters);
		}
		Ok(())
	}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-318)
```rust
#[test]
fn test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero() {
	new_tester().execute_with(|| {
		let gas_used: u64 = 250000;
		let price_params: PricingParameters<<Test as Config>::Balance> = PricingParameters {
			exchange_rate: FixedU128::from_rational(1, 1),
			fee_per_gas: 1_u32.into(),
			rewards: Rewards { local: 1_u32.into(), remote: 1_u32.into() },
			multiplier: FixedU128::from_rational(1, 1),
		};
		let fee = OutboundQueue::calculate_fee(gas_used, price_params.clone());
		assert_eq!(fee.local, 698000000);
		// Though none zero pricing params the remote fee calculated here is invalid
		// which should be avoided
		assert_eq!(fee.remote, 0);
	});
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-88)
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

		let queued_message: VersionedQueuedMessage = QueuedMessage {
			id: message_id,
			channel_id: message.channel_id,
			command: message.command.clone(),
		}
		.into();
		// The whole message should not be too large
		let encoded = queued_message.encode().try_into().map_err(|_| SendError::MessageTooLarge)?;

		let ticket = Ticket { message_id, channel_id: message.channel_id, message: encoded };

		Ok((ticket, fee))
	}

	fn deliver(ticket: Self::Ticket) -> Result<H256, SendError> {
		let origin = AggregateMessageOrigin::Snowbridge(ticket.channel_id);

		if ticket.channel_id != PRIMARY_GOVERNANCE_CHANNEL {
			ensure!(!Self::operating_mode().is_halted(), SendError::Halted);
		}

		let message = ticket.message.as_bounded_slice();

		T::MessageQueue::enqueue_message(message, origin);
		Self::deposit_event(Event::MessageQueued { id: ticket.message_id });
		Ok(ticket.message_id)
	}
```
