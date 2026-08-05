Audit Report

## Title
Integer-division rounding in Snowbridge outbound-queue `calculate_fee` can silently zero out the relayer fee, allowing underpriced Ethereum-bound messages - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

## Summary
`Pallet::calculate_fee` computes the remote-side fee for Ethereum message execution via `calculate_remote_fee`, `FixedU128` scaling, and `convert_from_ether_decimals`, the last of which performs a truncating integer division by `10^(18 - Decimals)`. With non-zero but small pricing parameters, this truncation can produce `Fee.remote == 0`, and `SendMessage::validate` accepts this degenerate value with no minimum-fee floor check, allowing a message requiring real Ethereum gas execution to be enqueued without reserving any relayer compensation.

## Finding Description
The code path is exactly as cited: `calculate_fee` [1](#0-0)  computes the remote wei fee, reinterprets it as `FixedU128`, applies `multiplier`/`exchange_rate`, and passes the result through `convert_from_ether_decimals`, which truncates via `checked_div` [2](#0-1) . Because Rust integer division truncates toward zero, any pre-division value smaller than `denom` (`10^8` for a 10-decimal chain) collapses to `0`.

The pallet's own test suite explicitly reproduces and flags this: with `exchange_rate = 1`, `fee_per_gas = 1`, `rewards.remote = 1`, `multiplier = 1`, `fee.remote` computes to exactly `0`, and the test comment states "though non zero pricing params the remote fee calculated here is invalid which should be avoided" [3](#0-2) .

`PricingParameters::validate()` only rejects parameters that are literally zero (`exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, `multiplier`) — it never inspects the *derived* fee outcome, so it cannot catch this rounding-to-zero case [4](#0-3) .

`SendMessage::validate` in `send_message_impl.rs` calls `Self::calculate_fee(gas_used_at_most, T::PricingParameters::get())` and uses the resulting `Fee` directly as the charge before issuing the ticket and enqueuing the message — there is no check that `fee.remote > 0` [5](#0-4) . Once ticketed, `deliver` unconditionally enqueues the message for eventual Ethereum-side execution [6](#0-5) , and the committed message later carries a nonzero `max_dispatch_gas`/`max_fee_per_gas` for relayers to execute on Ethereum [7](#0-6) , i.e., real gas-consuming work is committed with zero reserved remote compensation.

## Impact Explanation
This matches the "public underpriced work that degrades block production or stalls bridge processing" impact category. When the remote fee rounds to zero, ordinary (unprivileged) message senders exporting XCM to Ethereum through this pallet pay only the `local` processing fee while the message is still committed for on-chain Ethereum execution, leaving relayers without gas refund or reward for that message. At scale this disincentivizes relaying and can allow backlog accumulation in the outbound queue, degrading bridge throughput. The corrupted value is `Fee.remote`, which the code path treats as valid despite being an artifact of truncating division rather than an intentional zero fee.

## Likelihood Explanation
This does not require a malicious actor. It only requires `PricingParameters` (set by governance via `snowbridge-pallet-system`, not necessarily maliciously) to combine into a small enough `RemoteFeeAdjusted` value that division by `10^(18-Decimals)` truncates to zero — a condition the pallet's own regression test demonstrates occurs with realistic-looking non-zero parameters (`exchange_rate=1`, `fee_per_gas=1`, `reward=1`, `multiplier=1`). `validate()` provides no signal that the resulting fee could be zero, so this misconfiguration is not detectable at parameter-set time, and any ordinary sender can then trigger it repeatably by sending any qualifying command message.

## Recommendation
Add a floor check in `calculate_fee`/`convert_from_ether_decimals` that rejects the computed fee (or rounds up instead of truncating) whenever `gas_used_at_most > 0` and the resulting `remote` component would be `0`, ensuring senders can never enqueue gas-consuming Ethereum work while reserving zero relayer compensation.

## Proof of Concept
The pallet's existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` in `bridges/snowbridge/pallets/outbound-queue/src/test.rs` reproduces this directly: calling `OutboundQueue::calculate_fee(250000, price_params)` with all-non-zero `price_params` yields `fee.remote == 0` [3](#0-2) . This is the same `fee` value `SendMessage::validate` would use to charge the sender and issue the ticket [8](#0-7) , confirming the message would be enqueued with zero reserved remote/relayer compensation.

### Citations

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L332-352)
```rust
			let pricing_params = T::PricingParameters::get();
			let command = queued_message.command.index();
			let params = queued_message.command.abi_encode();
			let max_dispatch_gas =
				T::GasMeter::maximum_dispatch_gas_used_at_most(&queued_message.command);
			let reward = pricing_params.rewards.remote;

			// Construct the final committed message
			let message = CommittedMessage {
				channel_id: queued_message.channel_id,
				nonce,
				command,
				params,
				max_dispatch_gas,
				max_fee_per_gas: pricing_params
					.fee_per_gas
					.try_into()
					.defensive_unwrap_or(u128::MAX),
				reward: reward.try_into().defensive_unwrap_or(u128::MAX),
				id: queued_message.id,
			};
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L368-393)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/lib.rs (L411-418)
```rust
		// 1 DOT has 10 digits of precision
		// 1 KSM has 12 digits of precision
		// 1 ETH has 18 digits of precision
		pub(crate) fn convert_from_ether_decimals(value: u128) -> T::Balance {
			let decimals = ETHER_DECIMALS.saturating_sub(T::Decimals::get()) as u32;
			let denom = 10u128.saturating_pow(decimals);
			value.checked_div(denom).expect("divisor is non-zero; qed").into()
		}
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/test.rs (L303-319)
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

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L41-74)
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
```

**File:** bridges/snowbridge/pallets/outbound-queue/src/send_message_impl.rs (L76-88)
```rust
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
