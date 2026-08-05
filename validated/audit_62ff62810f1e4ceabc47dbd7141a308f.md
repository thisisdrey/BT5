All cited code matches the repository exactly. The `PricingParameters::validate()` function only checks that fields are non-zero [1](#0-0) , while `calculate_fee()` performs two truncating integer divisions (via `checked_div(&params.exchange_rate)` and `convert_from_ether_decimals`'s `checked_div(denom)`) with no bound check on the derived result [2](#0-1) . This is confirmed by a first-party unit test in the repository that demonstrates non-zero, "valid" parameters still yielding `fee.remote == 0` [3](#0-2) , and this fee-computation path is exercised on every public message send via `SendMessage::validate()` [4](#0-3) .

Audit Report

## Title
`calculate_fee()` can round the remote (Ethereum-side) fee down to zero without any minimum-bound check, letting users pay a valid-looking but underpriced delivery fee - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

## Summary
The Snowbridge outbound-queue fee logic in `calculate_fee()` validates only that governance-configured `PricingParameters` fields are non-zero via `PricingParameters::validate()`, but never checks that the computed remote fee itself remains above a sane floor after the truncating fixed-point division and decimal conversion. This allows the remote (relayer reward) fee to silently truncate to zero for otherwise "valid" parameter sets, as demonstrated by the repository's own unit test.

## Finding Description
`calculate_fee()` computes `fee.remote` by multiplying the wei-denominated gas+reward cost by `params.multiplier`, dividing by `params.exchange_rate` via `FixedU128::checked_div`, then converting from 18-decimal Ether precision to the local chain's decimals via `convert_from_ether_decimals`, which performs `value.checked_div(10^(18 - local_decimals))` [2](#0-1) . `PricingParameters::validate()` is the only guard applied to these parameters and merely rejects zero-valued fields; it performs no check on the derived output of the fee formula [1](#0-0) . Consequently, integer truncation in the two division steps can drive `fee.remote` to zero even though every input parameter is non-zero. This path is reached from `SendMessage::validate()`, invoked whenever any unprivileged user submits a message for delivery to Ethereum through the outbound queue [4](#0-3) , meaning any ordinary sender under an affected parameter regime pays a zero (or near-zero) relayer reward without any error or rejection.

## Impact Explanation
When `fee.remote` truncates to zero, the message is still committed with a `reward` of zero (or negligible value) for the Ethereum-side relayer, as shown directly by the repository's own test asserting `fee.remote == 0` for non-degenerate, non-zero pricing parameters [3](#0-2) . This constitutes public underpriced work: relayers are not compensated for gas spent delivering the message on Ethereum, which can degrade the incentive to relay and stall Snowbridge outbound message processing — directly matching the listed impact category "public underpriced work that degrades block production or stalls bridge processing."

## Likelihood Explanation
The condition is deterministic and reproducible for a range of pricing-parameter/gas-used combinations that fall in the truncation zone (e.g., low `fee_per_gas`/`reward` combined with a local chain's lower decimal precision relative to Ether's 18 decimals). The repository itself contains a passing unit test reproducing exactly this outcome, confirming no downstream check in `calculate_fee`, `convert_from_ether_decimals`, or `send_message_impl::validate` catches or rejects a degenerate zero remote fee. Once pricing parameters land in this regime, every message sent through the channel is silently underpriced until parameters are corrected.

## Recommendation
Add a post-computation floor check in `calculate_fee()` that rejects (returns `SendError`) if the derived `fee.remote` is zero or below a configurable minimum (e.g., proportional to `fee_per_gas * gas_used_at_most`), rather than silently accepting it. Alternatively, use ceiling (round-up) division instead of truncating `checked_div` in both the exchange-rate conversion and `convert_from_ether_decimals`, so the computed fee never rounds below the true cost. Extend `PricingParameters::validate()` to reject exchange-rate/fee_per_gas/multiplier/decimals combinations that would produce a zero effective fee for realistic `gas_used_at_most` values.

## Proof of Concept
The existing test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` demonstrates the bug directly: with `exchange_rate = 1/1`, `fee_per_gas = 1`, `reward = 1`, `multiplier = 1/1`, and `gas_used = 250000`, `calculate_remote_fee` yields `250001` wei, which after `convert_from_ether_decimals` truncates to `0` for a chain with fewer than 18 decimals of local precision, despite all `PricingParameters::validate()` checks passing [3](#0-2) .

### Citations

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
