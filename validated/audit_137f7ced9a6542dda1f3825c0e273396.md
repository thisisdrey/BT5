Audit Report

## Title
Truncating integer division in `convert_from_ether_decimals` underprices Snowbridge outbound-queue fees, allowing free/cheap spam of Ethereum-bound messages - (File: bridges/snowbridge/pallets/outbound-queue/src/lib.rs)

## Summary
`Pallet::<T>::calculate_fee` computes the remote (Ethereum-side) fee component in 18-decimal WAD precision and rescales it to the local chain's token precision via `convert_from_ether_decimals`, which performs a plain floor-division (`value.checked_div(denom)`) with no rounding-up and no minimum-fee floor. Any WAD-scaled value smaller than `denom` (`1e6` for 12-decimal chains, `1e8` for 10-decimal chains) collapses to `0`, meaning a message can be queued for Ethereum-side relaying while the remote fee charged to the sender is zero.

## Finding Description
`calculate_fee` computes the remote fee entirely in ether/WAD units via `calculate_remote_fee`, multiplies by `params.multiplier`, divides by `params.exchange_rate`, then calls `Self::convert_from_ether_decimals(fee)` to rescale to local precision: [1](#0-0) . `convert_from_ether_decimals` computes `denom = 10^(18 - T::Decimals)` and performs `value.checked_div(denom)` — a floor division with no rounding or minimum-fee safeguard: [2](#0-1) . `T::Decimals` is constrained only to `10` or `12` by `integrity_test`, so `denom` is `1e8` or `1e6` respectively. `PricingParameters::validate` only rejects exactly-zero `exchange_rate`, `fee_per_gas`, `rewards.local`, `rewards.remote`, or `multiplier`; it does not reject combinations that, while individually non-zero, produce a WAD-scaled fee below the precision threshold: [3](#0-2) . This exact degenerate case is demonstrated by an existing unit test using non-zero parameters (`exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = 1`) whose comment explicitly states the resulting zero remote fee "should be avoided": [4](#0-3) . The computed fee is exactly what `SendMessage::validate` charges the sender before enqueueing the message for relaying (`Fee.local` + `Fee.remote`), so a zero `Fee.remote` means the message is accepted and queued with no compensation for the real Ethereum-side gas a relayer must pay.

## Impact Explanation
When the current governance-set `PricingParameters` (a legitimate, non-malicious configuration state, since only non-zero values are enforced) fall into this precision-loss regime, any unprivileged user submitting Ethereum-bound messages pays zero remote fee while relayers still incur real Ethereum gas costs to execute the message. This matches the accepted impact category "public underpriced work that degrades block production or stalls bridge processing" — repeated exploitation degrades relayer incentives and can congest/spam the outbound bridge pipeline.

## Likelihood Explanation
The bug is unconditionally present in `convert_from_ether_decimals` and is reachable by any unprivileged sender through `SendMessage::validate` whenever the currently active `PricingParameters` produce a WAD-scaled remote fee below `denom` (`1e6`/`1e8`). Under typical realistic mock/production-style parameters (e.g. `fee_per_gas = 20 gwei`, `rewards.remote = 1 meth`, `exchange_rate = 1/400`) the computed value is far above the threshold, so exploitation requires the live parameters to drift into an unusually low-fee regime (low `fee_per_gas`/`reward`, or high `exchange_rate`). This is plausible but not guaranteed under normal market-driven governance updates, and there is no code enforcing a floor to prevent it — as confirmed by the existing test that reproduces exactly this condition with non-zero but small inputs.

## Recommendation
- Change `convert_from_ether_decimals` to round up (ceiling division) rather than truncate, ensuring any non-zero pre-scaling value never collapses to a zero local fee.
- Enforce a protocol-level non-zero minimum fee, or reject processing of messages whose computed local fee rounds to zero.
- Extend `PricingParameters::validate` (or the `set_pricing_parameters` extrinsic) to reject configurations that would cause the computed fee to round to zero for the configured `T::Decimals`.

## Proof of Concept
The existing repository test `test_calculate_fees_with_valid_exchange_rate_but_remote_fee_calculated_as_zero` reproduces this directly: with `gas_used = 250000`, `exchange_rate = 1/1`, `fee_per_gas = 1`, `rewards = {local: 1, remote: 1}`, `multiplier = 1/1`, `OutboundQueue::calculate_fee` returns `fee.remote == 0` despite all pricing parameters being non-zero, because the pre-division WAD value (`250001`) is below the 12-decimal-chain divisor `1e6`. [4](#0-3)  This message would still be accepted and queued via `SendMessage::validate`/`do_process_message` for relayer execution on Ethereum with zero remote-fee compensation. [5](#0-4)

### Citations

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
